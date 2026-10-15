import { Controller, Get } from '@nestjs/common';
import { ApiTags, ApiOperation, ApiResponse } from '@nestjs/swagger';
import { WebhookService } from './webhook.service';
import { Webhook } from './entities/webhook.entity';
import { OutboxStats, WebhookOutboxService } from './webhook-outbox.service';

@ApiTags('webhooks')
@Controller('webhooks')
export class WebhooksListController {
  constructor(
    private readonly webhookService: WebhookService,
    private readonly outbox: WebhookOutboxService,
  ) {}

  @Get()
  @ApiOperation({ summary: 'List all webhooks across all sessions' })
  @ApiResponse({
    status: 200,
    description: 'List of all webhooks',
  })
  async findAll(): Promise<Webhook[]> {
    return this.webhookService.findAll();
  }

  @Get('outbox')
  @ApiOperation({ summary: 'Undelivered events waiting for retry, per webhook' })
  @ApiResponse({ status: 200, description: 'Pending count, oldest event and last error per webhook' })
  async outboxStats(): Promise<OutboxStats[]> {
    return this.outbox.stats();
  }
}
