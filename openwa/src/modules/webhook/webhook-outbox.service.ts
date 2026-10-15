import { Injectable, OnModuleDestroy, OnModuleInit } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { ConfigService } from '@nestjs/config';
import { Repository } from 'typeorm';
import { Webhook } from './entities/webhook.entity';
import { WebhookOutbox } from './entities/webhook-outbox.entity';
import type { WebhookPayload } from './webhook.service';
import { buildWebhookHeaders, isPermanentFailure, postWebhook, WebhookHttpError } from './utils/post-webhook.util';
import { createLogger } from '../../common/services/logger.service';

/** Sends a WhatsApp text through a session; registered by SessionService. */
export type MessageSender = (sessionId: string, chatId: string, text: string) => Promise<unknown>;

interface Outage {
  since: Date;
  alertedAt?: Date;
  delivered: number;
  dropped: number;
  lastError?: string;
}

export interface OutboxStats {
  webhookId: string;
  pending: number;
  oldest: Date | null;
  lastError: string | null;
  alerted: boolean;
}

/**
 * Keeps webhook events that failed direct delivery and replays them, oldest
 * first, until the endpoint takes them. Alerts over WhatsApp when delivery
 * has been failing for a while, and again when it recovers.
 */
@Injectable()
export class WebhookOutboxService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = createLogger('WebhookOutbox');
  private readonly outages = new Map<string, Outage>();
  private timer?: NodeJS.Timeout;
  private draining = false;
  private sendMessage?: MessageSender;

  constructor(
    @InjectRepository(WebhookOutbox, 'data')
    private readonly outboxRepository: Repository<WebhookOutbox>,
    @InjectRepository(Webhook, 'data')
    private readonly webhookRepository: Repository<Webhook>,
    private readonly configService: ConfigService,
  ) {}

  onModuleInit(): void {
    const intervalMs = this.configService.get<number>('webhook.outbox.intervalMs', 30000);
    this.timer = setInterval(() => void this.drain(), intervalMs);
    this.timer.unref();
  }

  onModuleDestroy(): void {
    if (this.timer) clearInterval(this.timer);
  }

  setMessageSender(sender: MessageSender): void {
    this.sendMessage = sender;
  }

  async hasPending(webhookId: string): Promise<boolean> {
    return this.outboxRepository.exists({ where: { webhookId } });
  }

  /** `error` is the delivery failure; omit it when only queueing behind earlier events. */
  async add(webhook: Webhook, payload: WebhookPayload, error?: string): Promise<void> {
    await this.outboxRepository.save(
      this.outboxRepository.create({
        webhookId: webhook.id,
        sessionId: webhook.sessionId,
        event: payload.event,
        payload,
        attempts: 0,
        rejections: 0,
        lastError: error ?? null,
        createdAt: new Date(),
      }),
    );
    const outage = this.outageFor(webhook.id, new Date());
    if (error) outage.lastError = error;
    this.logger.warn(`Kept undelivered ${payload.event} for webhook ${webhook.id}; will retry`, {
      webhookId: webhook.id,
      deliveryId: payload.deliveryId,
      action: 'webhook_outbox_added',
    });
  }

  async drain(): Promise<void> {
    if (this.draining) return;
    this.draining = true;
    try {
      const rows = await this.outboxRepository
        .createQueryBuilder('o')
        .select('o.webhookId', 'webhookId')
        .distinct(true)
        .getRawMany<{ webhookId: string }>();
      const webhookIds = new Set([...rows.map(r => r.webhookId), ...this.outages.keys()]);

      for (const webhookId of webhookIds) {
        await this.drainWebhook(webhookId);
        await this.updateAlert(webhookId);
      }
    } catch (error) {
      this.logger.error('Webhook outbox drain failed', String(error), { action: 'webhook_outbox_drain_failed' });
    } finally {
      this.draining = false;
    }
  }

  async stats(): Promise<OutboxStats[]> {
    const rows = await this.outboxRepository
      .createQueryBuilder('o')
      .select('o.webhookId', 'webhookId')
      .addSelect('COUNT(*)', 'pending')
      .addSelect('MIN(o.id)', 'oldestId')
      .groupBy('o.webhookId')
      .getRawMany<{ webhookId: string; pending: string | number; oldestId: number }>();

    return Promise.all(
      rows.map(async row => {
        const oldest = await this.outboxRepository.findOne({ where: { id: row.oldestId } });
        return {
          webhookId: row.webhookId,
          pending: Number(row.pending),
          oldest: oldest?.createdAt ?? null,
          lastError: oldest?.lastError ?? null,
          alerted: !!this.outages.get(row.webhookId)?.alertedAt,
        };
      }),
    );
  }

  private outageFor(webhookId: string, since: Date): Outage {
    let outage = this.outages.get(webhookId);
    if (!outage) {
      outage = { since, delivered: 0, dropped: 0 };
      this.outages.set(webhookId, outage);
    }
    return outage;
  }

  private async drainWebhook(webhookId: string): Promise<void> {
    const entries = await this.outboxRepository.find({
      where: { webhookId },
      order: { id: 'ASC' },
      take: this.configService.get<number>('webhook.outbox.batchSize', 200),
    });
    if (entries.length === 0) return;

    const outage = this.outageFor(webhookId, entries[0].createdAt);
    const webhook = await this.webhookRepository.findOne({ where: { id: webhookId } });
    if (!webhook || !webhook.active) {
      const { affected } = await this.outboxRepository.delete({ webhookId });
      outage.dropped += affected ?? 0;
      this.logger.warn(`Dropped ${affected ?? 0} kept events: webhook ${webhookId} is deleted or inactive`, {
        webhookId,
        action: 'webhook_outbox_dropped',
      });
      return;
    }

    const maxAgeMs = this.configService.get<number>('webhook.outbox.maxAgeHours', 168) * 3600_000;
    const timeoutMs = this.configService.get<number>('webhook.timeout', 90000);

    for (const entry of entries) {
      if (Date.now() - entry.createdAt.getTime() > maxAgeMs) {
        await this.drop(entry, outage, 'older than the outbox max age');
        continue;
      }

      const headers = {
        ...buildWebhookHeaders(webhook, entry.payload),
        'X-OpenWA-Retry-Count': String(entry.attempts + 1),
      };
      try {
        await postWebhook(webhook, entry.payload, headers, timeoutMs);
        await this.outboxRepository.delete({ id: entry.id });
        outage.delivered += 1;
      } catch (error) {
        if (isPermanentFailure(error)) {
          await this.drop(entry, outage, String(error));
          continue;
        }
        const status = error instanceof WebhookHttpError ? error.status : undefined;
        await this.outboxRepository.update(
          { id: entry.id },
          { attempts: entry.attempts + 1, rejections: entry.rejections + (status ? 1 : 0), lastError: String(error) },
        );
        outage.lastError = String(error);
        // A 5xx is about this payload: keep it and try the rest. Anything else
        // (unreachable, timeout, 408/429) means the endpoint is down or
        // struggling, so stop and keep the order for the next pass.
        if (!status || status === 408 || status === 429) return;
      }
    }
  }

  private async drop(entry: WebhookOutbox, outage: Outage, reason: string): Promise<void> {
    await this.outboxRepository.delete({ id: entry.id });
    outage.dropped += 1;
    this.logger.error(`Gave up on kept ${entry.event} for webhook ${entry.webhookId}: ${reason}`, undefined, {
      webhookId: entry.webhookId,
      deliveryId: entry.payload.deliveryId,
      action: 'webhook_outbox_dropped',
    });
  }

  private async updateAlert(webhookId: string): Promise<void> {
    const outage = this.outages.get(webhookId);
    if (!outage) return;
    const pending = await this.outboxRepository.count({ where: { webhookId } });
    const webhook = await this.webhookRepository.findOne({ where: { id: webhookId } });

    if (pending === 0) {
      this.outages.delete(webhookId);
      if (outage.alertedAt && webhook) {
        const dropped = outage.dropped
          ? ` ${outage.dropped} could not be delivered and were dropped (see OpenWA logs).`
          : '';
        await this.alert(
          webhook,
          `✅ ${this.label(webhook)}: ${this.host(webhook)} is reachable again after ${formatDuration(Date.now() - outage.since.getTime())}. ` +
            `Delivered ${outage.delivered} kept messages.${dropped}`,
        );
      }
      return;
    }
    if (!webhook) return;

    const now = Date.now();
    const alertAfterMs = this.configService.get<number>('webhook.alert.afterMs', 120000);
    const reminderMs = this.configService.get<number>('webhook.alert.reminderMs', 6 * 3600_000);
    const due = outage.alertedAt
      ? now - outage.alertedAt.getTime() >= reminderMs
      : now - outage.since.getTime() >= alertAfterMs;
    if (!due) return;

    const sent = await this.alert(
      webhook,
      `⚠️ ${this.label(webhook)}: WhatsApp messages are not reaching ${this.host(webhook)} ` +
        `(failing since ${formatUtc(outage.since)}, ${pending} waiting). Last error: ${outage.lastError ?? 'unknown'}. ` +
        `They are kept and will be delivered automatically once it is back.`,
    );
    if (sent) outage.alertedAt = new Date(now);
  }

  /** True when at least one recipient got it (or there is nobody to alert). */
  private async alert(webhook: Webhook, text: string): Promise<boolean> {
    const phones = this.configService.get<string[]>('webhook.alert.phones', []);
    this.logger.warn(text, { webhookId: webhook.id, action: 'webhook_alert' });
    if (phones.length === 0 || !this.sendMessage) return true;

    let sent = false;
    for (const phone of phones) {
      try {
        await this.sendMessage(webhook.sessionId, `${phone}@c.us`, text);
        sent = true;
      } catch (error) {
        this.logger.error(`Webhook alert to ${phone} failed`, String(error), { action: 'webhook_alert_failed' });
      }
    }
    return sent;
  }

  private label(webhook: Webhook): string {
    return this.configService.get<string>('webhook.alert.label') || `OpenWA session ${webhook.sessionId}`;
  }

  private host(webhook: Webhook): string {
    try {
      return new URL(webhook.url).host;
    } catch {
      return webhook.url;
    }
  }
}

function formatUtc(date: Date): string {
  return `${date.toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}

function formatDuration(ms: number): string {
  const minutes = Math.round(ms / 60000);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours}h ${minutes % 60}m` : `${Math.floor(hours / 24)}d ${hours % 24}h`;
}
