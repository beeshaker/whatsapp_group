import { WhatsAppWebJsAdapter } from './whatsapp-web-js.adapter';
import { EngineStatus, IncomingReaction } from '../interfaces/whatsapp-engine.interface';

/* eslint-disable @typescript-eslint/no-unsafe-member-access, @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-assignment, @typescript-eslint/await-thenable --
 * This spec intentionally pokes the adapter's private fields (client, status, callbacks,
 * setupEventHandlers) via `(adapter as any)` to set up test doubles, and awaits mocked event
 * handlers whose signatures are `(...args: any[]) => void`. Both patterns are mandated by the
 * implementation plan and are safe in this test-only context. */

describe('WhatsAppWebJsAdapter', () => {
  describe('message_reaction handling', () => {
    function setup() {
      const handlers: Record<string, (...args: any[]) => void> = {};
      const mockClient = {
        on: jest.fn((event: string, handler: (...args: any[]) => void) => {
          handlers[event] = handler;
        }),
        getMessageById: jest.fn(),
      };

      const adapter = new WhatsAppWebJsAdapter({ sessionId: 'test', sessionDataPath: '/tmp/test' });
      (adapter as any).client = mockClient;
      (adapter as any).status = EngineStatus.READY;
      (adapter as any).setupEventHandlers();

      const onMessageReaction = jest.fn();
      (adapter as any).callbacks = { onMessageReaction };

      return { mockClient, handlers, onMessageReaction };
    }

    it('resolves targetTimestamp on a cache hit (getMessageById succeeds)', async () => {
      const { mockClient, handlers, onMessageReaction } = setup();
      mockClient.getMessageById.mockResolvedValue({ timestamp: 1782300000 });

      await handlers['message_reaction']({
        reaction: '👍',
        senderId: '254700000001@c.us',
        msgId: { _serialized: 'wa-msg-1', remote: '123@g.us', participant: '254711223344@c.us' },
      });

      const emitted: IncomingReaction = onMessageReaction.mock.calls[0][0];
      expect(emitted).toEqual({
        chatId: '123@g.us',
        emoji: '👍',
        senderId: '254700000001@c.us',
        targetMessageId: 'wa-msg-1',
        targetAuthor: '254711223344@c.us',
        targetTimestamp: 1782300000,
      });
    });

    it('omits targetTimestamp on a cache miss (getMessageById throws) but keeps targetAuthor', async () => {
      const { mockClient, handlers, onMessageReaction } = setup();
      mockClient.getMessageById.mockRejectedValue(new Error('message not in cache'));

      await handlers['message_reaction']({
        reaction: '👍',
        senderId: '254700000001@c.us',
        msgId: { _serialized: 'wa-msg-2', remote: '123@g.us', participant: '254711223344@c.us' },
      });

      const emitted: IncomingReaction = onMessageReaction.mock.calls[0][0];
      expect(emitted.targetAuthor).toBe('254711223344@c.us');
      expect(emitted.targetTimestamp).toBeUndefined();
    });

    it('falls back to msgId.remote as targetAuthor when participant is absent (non-group chat)', async () => {
      const { mockClient, handlers, onMessageReaction } = setup();
      mockClient.getMessageById.mockResolvedValue({ timestamp: 1782300001 });

      await handlers['message_reaction']({
        reaction: '👍',
        senderId: '254700000001@c.us',
        msgId: { _serialized: 'wa-msg-3', remote: '254711223344@c.us' },
      });

      const emitted: IncomingReaction = onMessageReaction.mock.calls[0][0];
      expect(emitted.targetAuthor).toBe('254711223344@c.us');
      expect(emitted.chatId).toBe('254711223344@c.us');
    });
  });

  describe('replyToMessage', () => {
    function setupChat(messages: any[]) {
      const chat = {
        fetchMessages: jest.fn().mockResolvedValue(messages),
        sendMessage: jest.fn().mockResolvedValue({ id: { _serialized: 'plain-1' }, timestamp: 999 }),
      };
      const mockClient = {
        getChatById: jest.fn().mockResolvedValue(chat),
      };
      const adapter = new WhatsAppWebJsAdapter({ sessionId: 'test', sessionDataPath: '/tmp/test' });
      (adapter as any).client = mockClient;
      (adapter as any).status = EngineStatus.READY;
      return { adapter, chat };
    }

    it('replies by exact message ID when found (unchanged behavior)', async () => {
      const targetMsg = {
        id: { _serialized: 'wa-quoted-1' },
        author: 'author@c.us',
        timestamp: 1700000000,
        reply: jest.fn().mockResolvedValue({ id: { _serialized: 'reply-1' }, timestamp: 1000 }),
      };
      const { adapter } = setupChat([targetMsg]);

      const result = await adapter.replyToMessage('123@g.us', 'wa-quoted-1', 'Hello');

      expect(targetMsg.reply).toHaveBeenCalledWith('Hello');
      expect(result).toEqual({ id: 'reply-1', timestamp: 1000 });
    });

    it('falls back to author+timestamp match when exact ID is absent', async () => {
      // Realistic shapes: whatsapp-web.js's ExtendedMessage.author is a full
      // JID ("<phone>@c.us"), but the backend's authorHint is the bare phone
      // number (reporter_phone is stored stripped of its JID suffix at
      // ingest). The match must normalize both sides to compare correctly.
      const targetMsg = {
        id: { _serialized: 'some-other-id' },
        author: '254711223344@c.us',
        timestamp: 1700000000,
        reply: jest.fn().mockResolvedValue({ id: { _serialized: 'reply-2' }, timestamp: 1001 }),
      };
      const { adapter } = setupChat([targetMsg]);

      const result = await adapter.replyToMessage(
        '123@g.us',
        'wa-quoted-missing',
        'Hello',
        '254711223344',
        1700000000,
        'Original snippet',
      );

      expect(targetMsg.reply).toHaveBeenCalledWith('Hello');
      expect(result).toEqual({ id: 'reply-2', timestamp: 1001 });
    });

    it('sends a plain message prefixed with the snippet when no match is found but hints were supplied', async () => {
      const { adapter, chat } = setupChat([]);

      const result = await adapter.replyToMessage(
        '123@g.us',
        'wa-quoted-missing',
        'Hello',
        'author@c.us',
        1700000000,
        'Original snippet',
      );

      expect(chat.sendMessage).toHaveBeenCalledWith('> Original snippet\n\nHello');
      expect(result).toEqual({ id: 'plain-1', timestamp: 999 });
    });

    it('throws when no hints are supplied at all and no exact match is found (back-compat)', async () => {
      const { adapter } = setupChat([]);

      await expect(adapter.replyToMessage('123@g.us', 'wa-quoted-missing', 'Hello')).rejects.toThrow(
        'Message wa-quoted-missing not found',
      );
    });
  });
});
