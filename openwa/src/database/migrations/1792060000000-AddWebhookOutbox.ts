import { MigrationInterface, QueryRunner } from 'typeorm';

/**
 * Persistent outbox for webhook events that failed direct delivery, so an
 * unreachable backend delays messages instead of losing them.
 */
export class AddWebhookOutbox1792060000000 implements MigrationInterface {
  name = 'AddWebhookOutbox1792060000000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    if (queryRunner.connection.options.type === 'postgres') {
      await queryRunner.query(
        `CREATE TABLE "webhook_outbox" ("id" SERIAL PRIMARY KEY, "webhookId" varchar NOT NULL, "sessionId" varchar NOT NULL, "event" varchar NOT NULL, "payload" jsonb NOT NULL, "attempts" integer NOT NULL DEFAULT 0, "rejections" integer NOT NULL DEFAULT 0, "lastError" text, "createdAt" timestamp NOT NULL)`,
      );
    } else {
      await queryRunner.query(
        `CREATE TABLE "webhook_outbox" ("id" integer PRIMARY KEY AUTOINCREMENT NOT NULL, "webhookId" varchar NOT NULL, "sessionId" varchar NOT NULL, "event" varchar NOT NULL, "payload" text NOT NULL, "attempts" integer NOT NULL DEFAULT (0), "rejections" integer NOT NULL DEFAULT (0), "lastError" text, "createdAt" text NOT NULL)`,
      );
    }
    await queryRunner.query(`CREATE INDEX "IDX_webhook_outbox_webhook_id" ON "webhook_outbox" ("webhookId", "id")`);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`DROP INDEX "IDX_webhook_outbox_webhook_id"`);
    await queryRunner.query(`DROP TABLE "webhook_outbox"`);
  }
}
