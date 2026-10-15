import { Entity, Column, PrimaryGeneratedColumn, Index } from 'typeorm';
import { DateTransformer } from '../../../common/transformers/date.transformer';
import { jsonColumnType, dateColumnType } from '../../../common/utils/column-types';
import type { WebhookPayload } from '../webhook.service';

/**
 * A webhook event that could not be delivered directly. Kept until the
 * endpoint accepts it, then deleted. The increment id is the replay order.
 */
@Entity('webhook_outbox')
@Index('IDX_webhook_outbox_webhook_id', ['webhookId', 'id'])
export class WebhookOutbox {
  @PrimaryGeneratedColumn('increment')
  id: number;

  @Column({ type: 'varchar' })
  webhookId: string;

  @Column({ type: 'varchar' })
  sessionId: string;

  @Column({ type: 'varchar' })
  event: string;

  @Column({ type: jsonColumnType() })
  payload: WebhookPayload;

  // Every failed replay, including "endpoint unreachable".
  @Column({ type: 'int', default: 0 })
  attempts: number;

  // Only failed replays where the endpoint answered with an error status;
  // a payload the endpoint keeps rejecting is eventually given up on.
  @Column({ type: 'int', default: 0 })
  rejections: number;

  @Column({ type: 'text', nullable: true })
  lastError: string | null;

  @Column({ type: dateColumnType(), transformer: DateTransformer })
  createdAt: Date;
}
