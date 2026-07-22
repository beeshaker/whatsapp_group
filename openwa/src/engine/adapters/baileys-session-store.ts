import type { WAMessage } from '@whiskeysockets/baileys';

export interface StoredMessage {
  id: string;
  chatId: string;
  author?: string;
  timestamp: number;
  raw: WAMessage;
}

const DEFAULT_MAX_MESSAGES_PER_CHAT = 200;

/**
 * A bounded, in-memory, per-chat rolling buffer of recently seen messages.
 * Baileys doesn't cache chat/message history the way whatsapp-web.js does,
 * so replyToMessage's exact-ID and author+timestamp fallback lookups need
 * their own store, populated as messages arrive (see BaileysAdapter's
 * messages.upsert handling).
 */
export class BaileysSessionStore {
  private readonly messagesByChatId = new Map<string, StoredMessage[]>();

  constructor(private readonly maxMessagesPerChat: number = DEFAULT_MAX_MESSAGES_PER_CHAT) {}

  add(message: StoredMessage): void {
    const existing = this.messagesByChatId.get(message.chatId) ?? [];
    existing.push(message);
    if (existing.length > this.maxMessagesPerChat) {
      existing.shift();
    }
    this.messagesByChatId.set(message.chatId, existing);
  }

  findById(chatId: string, messageId: string): StoredMessage | undefined {
    return this.messagesByChatId.get(chatId)?.find(m => m.id === messageId);
  }

  findByAuthorAndTimestamp(chatId: string, authorHint: string, timestampHint: number): StoredMessage | undefined {
    return this.messagesByChatId
      .get(chatId)
      ?.find(m => m.author?.split('@')[0] === authorHint && m.timestamp === timestampHint);
  }
}
