import { Module, DynamicModule, Type } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { Webhook } from './entities/webhook.entity';
import { WebhookOutbox } from './entities/webhook-outbox.entity';
import { WebhookService } from './webhook.service';
import { WebhookOutboxService } from './webhook-outbox.service';
import { WebhookController } from './webhook.controller';
import { WebhooksListController } from './webhooks-list.controller';

// Only import QueueModule if explicitly enabled to avoid Redis connection errors
const queueModules: Array<Type | DynamicModule> = [];
if (process.env.QUEUE_ENABLED === 'true') {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const queueModule = require('../queue/queue.module') as {
    QueueModule: Type;
  };
  queueModules.push(queueModule.QueueModule);
}

@Module({
  imports: [TypeOrmModule.forFeature([Webhook, WebhookOutbox], 'data'), ...queueModules],
  controllers: [WebhookController, WebhooksListController],
  providers: [WebhookService, WebhookOutboxService],
  exports: [WebhookService, WebhookOutboxService],
})
export class WebhookModule {}
