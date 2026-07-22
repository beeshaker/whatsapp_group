import { BaileysSessionStore, StoredMessage } from './baileys-session-store';

function makeMessage(overrides: Partial<StoredMessage> = {}): StoredMessage {
  return {
    id: 'msg-1',
    chatId: '123@g.us',
    author: '254711223344@c.us',
    timestamp: 1700000000,
    raw: { key: { id: 'msg-1' } } as StoredMessage['raw'],
    ...overrides,
  };
}

describe('BaileysSessionStore', () => {
  describe('add / findById', () => {
    it('finds a message by chatId and id after adding it', () => {
      const store = new BaileysSessionStore();
      const message = makeMessage();
      store.add(message);

      expect(store.findById('123@g.us', 'msg-1')).toBe(message);
    });

    it('returns undefined for a chatId that has no messages', () => {
      const store = new BaileysSessionStore();
      expect(store.findById('999@g.us', 'msg-1')).toBeUndefined();
    });

    it('returns undefined for an id that was never added in that chat', () => {
      const store = new BaileysSessionStore();
      store.add(makeMessage({ id: 'msg-1' }));
      expect(store.findById('123@g.us', 'msg-does-not-exist')).toBeUndefined();
    });

    it('keeps messages from different chats separate', () => {
      const store = new BaileysSessionStore();
      store.add(makeMessage({ id: 'msg-1', chatId: '123@g.us' }));
      store.add(makeMessage({ id: 'msg-1', chatId: '456@g.us', timestamp: 1700000001 }));

      expect(store.findById('123@g.us', 'msg-1')?.timestamp).toBe(1700000000);
      expect(store.findById('456@g.us', 'msg-1')?.timestamp).toBe(1700000001);
    });
  });

  describe('eviction', () => {
    it('evicts the oldest message once a chat exceeds maxMessagesPerChat', () => {
      const store = new BaileysSessionStore(2);
      store.add(makeMessage({ id: 'msg-1' }));
      store.add(makeMessage({ id: 'msg-2' }));
      store.add(makeMessage({ id: 'msg-3' }));

      expect(store.findById('123@g.us', 'msg-1')).toBeUndefined();
      expect(store.findById('123@g.us', 'msg-2')).toBeDefined();
      expect(store.findById('123@g.us', 'msg-3')).toBeDefined();
    });
  });

  describe('findByAuthorAndTimestamp', () => {
    it('matches on bare-number author (ignoring the @c.us suffix) and exact timestamp', () => {
      const store = new BaileysSessionStore();
      const message = makeMessage({ author: '254711223344@c.us', timestamp: 1700000000 });
      store.add(message);

      expect(store.findByAuthorAndTimestamp('123@g.us', '254711223344', 1700000000)).toBe(message);
    });

    it('returns undefined when the timestamp does not match', () => {
      const store = new BaileysSessionStore();
      store.add(makeMessage({ author: '254711223344@c.us', timestamp: 1700000000 }));

      expect(store.findByAuthorAndTimestamp('123@g.us', '254711223344', 1700000001)).toBeUndefined();
    });

    it('returns undefined when the author does not match', () => {
      const store = new BaileysSessionStore();
      store.add(makeMessage({ author: '254711223344@c.us', timestamp: 1700000000 }));

      expect(store.findByAuthorAndTimestamp('123@g.us', '254799999999', 1700000000)).toBeUndefined();
    });
  });
});
