import { ConfigService } from '@nestjs/config';
import { DataSource, Repository } from 'typeorm';
import { WebhookOutboxService } from './webhook-outbox.service';
import { WebhookOutbox } from './entities/webhook-outbox.entity';
import { Webhook } from './entities/webhook.entity';
import { WebhookPayload } from './webhook.service';
import { Session } from '../session/entities/session.entity';
import { AddMessageStatus1770108659848 } from '../../database/migrations/1770108659848-AddMessageStatus';
import { AddUuidDefaultsForPostgres1779235200000 } from '../../database/migrations/1779235200000-AddUuidDefaultsForPostgres';
import { AddWebhookOutbox1792060000000 } from '../../database/migrations/1792060000000-AddWebhookOutbox';

const ok = { ok: true, status: 202, statusText: 'Accepted' };
const unreachable = new TypeError('fetch failed');

function payload(n: number): WebhookPayload {
  return {
    event: 'message.received',
    timestamp: new Date().toISOString(),
    sessionId: 'sess-1',
    idempotencyKey: `key-${n}`,
    deliveryId: `dlv-${n}`,
    data: { body: `message ${n}` },
  };
}

type FetchCall = [string, { headers: Record<string, string>; body: string }];
type SendCall = [string, string, string];

function sentBodies(fetchMock: jest.Mock): string[] {
  return (fetchMock.mock.calls as FetchCall[]).map(
    ([, init]) => (JSON.parse(init.body) as WebhookPayload).data.body as string,
  );
}

describe('WebhookOutboxService', () => {
  let dataSource: DataSource;
  let outboxRepo: Repository<WebhookOutbox>;
  let webhookRepo: Repository<Webhook>;
  let webhook: Webhook;
  let service: WebhookOutboxService;
  let sender: jest.Mock;
  let fetchMock: jest.Mock;
  let settings: Record<string, unknown>;

  beforeEach(async () => {
    // The real migrations, so the new table's DDL is exercised too.
    dataSource = new DataSource({
      type: 'sqlite',
      database: ':memory:',
      entities: [Session, Webhook, WebhookOutbox],
      migrations: [
        AddMessageStatus1770108659848,
        AddUuidDefaultsForPostgres1779235200000,
        AddWebhookOutbox1792060000000,
      ],
      migrationsRun: true,
    });
    await dataSource.initialize();
    outboxRepo = dataSource.getRepository(WebhookOutbox);
    webhookRepo = dataSource.getRepository(Webhook);

    await dataSource.getRepository(Session).save({ id: 'sess-1', name: 'opsgateway' });
    webhook = await webhookRepo.save(
      webhookRepo.create({
        sessionId: 'sess-1',
        url: 'http://backend.internal:8000/api/v1/ops/ingest',
        headers: { 'X-API-Key': 'k' },
      }),
    );

    settings = { 'webhook.alert.phones': ['254700000001'], 'webhook.alert.label': 'pixiilive' };
    const config = {
      get: jest.fn(<T>(key: string, def?: T) => (key in settings ? settings[key] : def)),
    } as unknown as ConfigService;

    service = new WebhookOutboxService(outboxRepo, webhookRepo, config);
    sender = jest.fn().mockResolvedValue(undefined);
    service.setMessageSender(sender);

    fetchMock = jest.fn().mockResolvedValue(ok);
    global.fetch = fetchMock as typeof global.fetch;
  });

  afterEach(async () => {
    await dataSource.destroy();
  });

  it('replays kept events oldest first with the current webhook headers, then empties', async () => {
    await service.add(webhook, payload(1), 'fetch failed');
    await service.add(webhook, payload(2), 'fetch failed');

    await service.drain();

    expect(sentBodies(fetchMock)).toEqual(['message 1', 'message 2']);
    expect((fetchMock.mock.calls as FetchCall[])[0][1].headers['X-API-Key']).toBe('k');
    expect(await outboxRepo.count()).toBe(0);
  });

  it('stops at the first unreachable attempt and keeps the order for next time', async () => {
    await service.add(webhook, payload(1), 'fetch failed');
    await service.add(webhook, payload(2), 'fetch failed');
    fetchMock.mockRejectedValue(unreachable);

    await service.drain();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const kept = await outboxRepo.find({ order: { id: 'ASC' } });
    expect(kept.map(k => k.payload.data.body)).toEqual(['message 1', 'message 2']);
    expect(kept[0].attempts).toBe(1);
    expect(kept[0].lastError).toContain('fetch failed');
  });

  it('keeps a payload the endpoint answers 5xx to, and still delivers the ones after it', async () => {
    await service.add(webhook, payload(1), 'x');
    await service.add(webhook, payload(2), 'x');
    fetchMock.mockResolvedValueOnce({ ok: false, status: 500, statusText: 'Internal Server Error' });

    await service.drain();

    const kept = await outboxRepo.find();
    expect(kept.map(k => k.payload.data.body)).toEqual(['message 1']);
    expect(kept[0].rejections).toBe(1);
  });

  it('drops payloads the endpoint refuses (4xx) and ones past the max age', async () => {
    await service.add(webhook, payload(1), 'x');
    await service.add(webhook, payload(2), 'x');
    await outboxRepo.update({ id: 2 }, { createdAt: new Date(Date.now() - 200 * 3600_000) });
    fetchMock.mockResolvedValueOnce({ ok: false, status: 401, statusText: 'Unauthorized' });

    await service.drain();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(await outboxRepo.count()).toBe(0);
  });

  it('drops kept events for a webhook that was deactivated', async () => {
    await service.add(webhook, payload(1), 'x');
    await webhookRepo.update({ id: webhook.id }, { active: false });

    await service.drain();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(await outboxRepo.count()).toBe(0);
  });

  it('stays quiet for a short blip that clears before the alert delay', async () => {
    await service.add(webhook, payload(1), 'fetch failed');

    await service.drain();

    expect(sender).not.toHaveBeenCalled();
  });

  it('alerts once when delivery keeps failing, then reports the recovery', async () => {
    settings['webhook.alert.afterMs'] = 0;
    await service.add(webhook, payload(1), 'fetch failed');
    await service.add(webhook, payload(2), 'fetch failed');
    fetchMock.mockRejectedValue(unreachable);

    await service.drain();
    await service.drain();

    expect(sender).toHaveBeenCalledTimes(1);
    const [sessionId, chatId, down] = (sender.mock.calls as SendCall[])[0];
    expect(sessionId).toBe('sess-1');
    expect(chatId).toBe('254700000001@c.us');
    expect(down).toContain('pixiilive');
    expect(down).toContain('backend.internal:8000');
    expect(down).toContain('2 waiting');
    expect((await service.stats())[0]).toMatchObject({ pending: 2, alerted: true });

    fetchMock.mockResolvedValue(ok);
    await service.drain();

    expect(sender).toHaveBeenCalledTimes(2);
    expect((sender.mock.calls as SendCall[])[1][2]).toContain('Delivered 2 kept messages');
    expect(await service.stats()).toEqual([]);
  });

  it('reports the real failure, not the events queued behind it', async () => {
    settings['webhook.alert.afterMs'] = 0;
    await service.add(webhook, payload(1), 'TypeError: fetch failed');
    await service.add(webhook, payload(2));
    fetchMock.mockRejectedValue(unreachable);

    await service.drain();

    expect((sender.mock.calls as SendCall[])[0][2]).toContain('Last error: TypeError: fetch failed');
  });

  it('retries the alert on the next pass if it could not be sent', async () => {
    settings['webhook.alert.afterMs'] = 0;
    await service.add(webhook, payload(1), 'fetch failed');
    fetchMock.mockRejectedValue(unreachable);
    sender.mockRejectedValueOnce(new Error('Session sess-1 is not running'));

    await service.drain();
    await service.drain();

    expect(sender).toHaveBeenCalledTimes(2);
    expect((await service.stats())[0].alerted).toBe(true);
  });

  it('picks up events kept before a restart', async () => {
    await service.add(webhook, payload(1), 'fetch failed');
    const restarted = new WebhookOutboxService(outboxRepo, webhookRepo, {
      get: jest.fn(<T>(_key: string, def?: T) => def),
    } as unknown as ConfigService);

    await restarted.drain();

    expect(sentBodies(fetchMock)).toEqual(['message 1']);
  });
});
