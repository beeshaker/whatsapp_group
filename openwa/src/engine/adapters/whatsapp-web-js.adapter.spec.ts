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
});
