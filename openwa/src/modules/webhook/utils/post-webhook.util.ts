import * as crypto from 'crypto';
import { Webhook } from '../entities/webhook.entity';
import type { WebhookPayload } from '../webhook.service';

/** The endpoint answered, but not with a 2xx. */
export class WebhookHttpError extends Error {
  constructor(
    readonly status: number,
    statusText: string,
  ) {
    super(`HTTP ${status}: ${statusText}`);
    this.name = 'WebhookHttpError';
  }
}

/**
 * A 4xx (other than timeout/rate limit) means the endpoint understood the
 * request and refused it; sending the same payload again won't help.
 */
export function isPermanentFailure(error: unknown): boolean {
  return (
    error instanceof WebhookHttpError && error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)
  );
}

export function signWebhookBody(body: string, secret: string): string {
  const hmac = crypto.createHmac('sha256', secret);
  hmac.update(body);
  return `sha256=${hmac.digest('hex')}`;
}

export function buildWebhookHeaders(webhook: Webhook, payload: WebhookPayload): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    'User-Agent': 'OpenWA-Webhook/1.0.0',
    'X-OpenWA-Event': payload.event,
    'X-OpenWA-Idempotency-Key': payload.idempotencyKey,
    'X-OpenWA-Delivery-Id': payload.deliveryId,
    'X-OpenWA-Retry-Count': '0',
    ...webhook.headers,
  };
}

/** One POST, no retries. Throws on a network error or a non-2xx response. */
export async function postWebhook(
  webhook: Webhook,
  payload: WebhookPayload,
  headers: Record<string, string>,
  timeoutMs: number,
): Promise<void> {
  const body = JSON.stringify(payload);
  if (webhook.secret && !headers['X-OpenWA-Signature']) {
    headers['X-OpenWA-Signature'] = signWebhookBody(body, webhook.secret);
  }

  const response = await fetch(webhook.url, {
    method: 'POST',
    headers,
    body,
    signal: AbortSignal.timeout(timeoutMs),
  });

  if (!response.ok) {
    throw new WebhookHttpError(response.status, response.statusText);
  }
}
