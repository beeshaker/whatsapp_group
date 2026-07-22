import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

jest.mock('@whiskeysockets/baileys', () => ({
  __esModule: true,
  default: jest.fn(),
  useMultiFileAuthState: jest.fn(),
  downloadMediaMessage: jest.fn(),
}));

import makeWASocket, { useMultiFileAuthState, downloadMediaMessage } from '@whiskeysockets/baileys';
import { BaileysAdapter } from './baileys.adapter';
import { EngineStatus } from '../interfaces/whatsapp-engine.interface';

const mockMakeWASocket = makeWASocket as jest.MockedFunction<typeof makeWASocket>;
const mockUseMultiFileAuthState = useMultiFileAuthState as jest.MockedFunction<typeof useMultiFileAuthState>;
const mockDownloadMediaMessage = downloadMediaMessage as jest.MockedFunction<typeof downloadMediaMessage>;

// Polls a predicate against real timers until it's true, instead of guessing a
// fixed delay -- used for the one assertion in this file (QR encoding) that
// depends on genuine async I/O (real `qrcode` -> zlib) rather than a mocked,
// already-resolved promise.
async function waitFor(predicate: () => boolean, timeoutMs = 2000, intervalMs = 10): Promise<void> {
  const start = Date.now();
  while (!predicate()) {
    if (Date.now() - start > timeoutMs) {
      throw new Error('waitFor: condition not met within timeout');
    }
    await new Promise(resolve => setTimeout(resolve, intervalMs));
  }
}

/* eslint-disable @typescript-eslint/no-unsafe-member-access, @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-assignment, @typescript-eslint/no-explicit-any --
 * This spec mocks @whiskeysockets/baileys's makeWASocket/useMultiFileAuthState wholesale and
 * builds a minimal fake WASocket (only the `ev`/`user` surface BaileysAdapter actually touches),
 * so several assertions and the fake socket itself are necessarily loosely typed. */

describe('BaileysAdapter', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'baileys-adapter-test-'));
    jest.clearAllMocks();
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  function setupMockSock() {
    const handlers: Record<string, (...args: any[]) => void> = {};
    const mockSock: any = {
      ev: {
        on: jest.fn((event: string, handler: (...args: any[]) => void) => {
          handlers[event] = handler;
        }),
        removeAllListeners: jest.fn(),
      },
      user: undefined,
      end: jest.fn().mockResolvedValue(undefined),
      logout: jest.fn().mockResolvedValue(undefined),
    };
    mockUseMultiFileAuthState.mockResolvedValue({ state: {} as any, saveCreds: jest.fn().mockResolvedValue(undefined) });
    mockMakeWASocket.mockReturnValue(mockSock);
    return { mockSock, handlers };
  }

  let lastHandlers: Record<string, (...args: any[]) => void> = {};

  function handlers_forceReady(adapter: BaileysAdapter, mockSock: any): void {
    lastHandlers = mockSock.ev.on.mock.calls.reduce((acc: any, [event, handler]: [string, any]) => {
      acc[event] = handler;
      return acc;
    }, {});
    mockSock.user = mockSock.user ?? { id: '000@lid', phoneNumber: '254700000000@s.whatsapp.net' };
    lastHandlers['connection.update']({ connection: 'open' });
  }

  describe('initialize', () => {
    it('creates the auth directory, wires up the socket with the auth state, and persists creds on creds.update', async () => {
      const { mockSock, handlers } = setupMockSock();
      const adapter = new BaileysAdapter({ sessionId: 'dunhill', authDir: tmpDir });

      await adapter.initialize({});

      const expectedAuthPath = path.join(tmpDir, 'dunhill');
      expect(fs.existsSync(expectedAuthPath)).toBe(true);
      expect(mockUseMultiFileAuthState).toHaveBeenCalledWith(expectedAuthPath);
      expect(mockMakeWASocket).toHaveBeenCalledWith({ auth: {} });

      handlers['creds.update']();
      const { saveCreds } = await mockUseMultiFileAuthState.mock.results[0].value;
      expect(saveCreds).toHaveBeenCalled();
      expect(mockSock.ev.on).toHaveBeenCalledWith('creds.update', expect.any(Function));
    });
  });

  describe('connection.update handling', () => {
    it('converts the raw qr string to a data URL and reports QR_READY', async () => {
      const { handlers } = setupMockSock();
      const adapter = new BaileysAdapter({ sessionId: 'test', authDir: tmpDir });
      const onQRCode = jest.fn();
      await adapter.initialize({ onQRCode });

      handlers['connection.update']({ qr: 'raw-qr-string' });
      // Unlike the other handlers in this suite, QR encoding goes through the
      // real `qrcode` package (not mocked), which renders a PNG via zlib on
      // Node's libuv threadpool -- genuine async I/O, not just a microtask.
      // A single `await new Promise(process.nextTick)` (as used elsewhere in
      // this file for mocked, already-resolved promises) is not enough to
      // let that complete, so this polls for the callback instead of guessing
      // a fixed delay (the real encode time varies run to run).
      await waitFor(() => onQRCode.mock.calls.length > 0);

      expect(onQRCode).toHaveBeenCalledWith(expect.stringMatching(/^data:image\/png;base64,/));
      expect(adapter.getStatus()).toBe(EngineStatus.QR_READY);
    });

    it('resolves phone number from phoneNumber (preferred over an opaque @lid id) and pushName from notify, on connection open', async () => {
      const { mockSock, handlers } = setupMockSock();
      mockSock.user = { id: 'AB12CD34@lid', phoneNumber: '254711223344:5@s.whatsapp.net', notify: 'Dunhill Bot' };
      const adapter = new BaileysAdapter({ sessionId: 'test', authDir: tmpDir });
      const onReady = jest.fn();
      await adapter.initialize({ onReady });

      handlers['connection.update']({ connection: 'open' });

      expect(onReady).toHaveBeenCalledWith('254711223344', 'Dunhill Bot');
      expect(adapter.getPhoneNumber()).toBe('254711223344');
      expect(adapter.getPushName()).toBe('Dunhill Bot');
      expect(adapter.getStatus()).toBe(EngineStatus.READY);
    });

    it('reports onDisconnected with the underlying error message on connection close', async () => {
      const { handlers } = setupMockSock();
      const adapter = new BaileysAdapter({ sessionId: 'test', authDir: tmpDir });
      const onDisconnected = jest.fn();
      await adapter.initialize({ onDisconnected });

      handlers['connection.update']({
        connection: 'close',
        lastDisconnect: { error: new Error('conflict'), date: new Date() },
      });

      expect(onDisconnected).toHaveBeenCalledWith('conflict');
      expect(adapter.getStatus()).toBe(EngineStatus.DISCONNECTED);
    });
  });

  describe('messages.upsert handling', () => {
    it('maps a plain group text message to type "chat" with the wwjs-compatible shape', async () => {
      const { handlers } = setupMockSock();
      const adapter = new BaileysAdapter({ sessionId: 'test', authDir: tmpDir });
      const onMessage = jest.fn();
      await adapter.initialize({ onMessage });

      handlers['messages.upsert']({
        type: 'notify',
        messages: [
          {
            key: { remoteJid: '123@g.us', participant: '254711223344@s.whatsapp.net', id: 'wa-1', fromMe: false },
            message: { conversation: 'Hello there' },
            messageTimestamp: 1782300000,
            pushName: 'Jane Doe',
          },
        ],
      });
      await new Promise(process.nextTick);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          id: 'wa-1',
          chatId: '123@g.us',
          body: 'Hello there',
          type: 'chat',
          author: '254711223344@c.us',
          notifyName: 'Jane Doe',
          isGroup: true,
          fromMe: false,
        }),
      );
    });

    it('prefers participantAlt (phone-number JID) over an opaque @lid participant', async () => {
      const { handlers } = setupMockSock();
      const adapter = new BaileysAdapter({ sessionId: 'test', authDir: tmpDir });
      const onMessage = jest.fn();
      await adapter.initialize({ onMessage });

      handlers['messages.upsert']({
        type: 'notify',
        messages: [
          {
            key: {
              remoteJid: '123@g.us',
              participant: 'AB12CD34@lid',
              participantAlt: '254711223344@s.whatsapp.net',
              id: 'wa-2',
              fromMe: false,
            },
            message: { conversation: 'hi' },
            messageTimestamp: 1782300001,
          },
        ],
      });
      await new Promise(process.nextTick);

      expect(onMessage).toHaveBeenCalledWith(expect.objectContaining({ author: '254711223344@c.us' }));
    });

    it('maps an imageMessage to type "image", uses the caption as body, and downloads the media as base64', async () => {
      const { handlers } = setupMockSock();
      mockDownloadMediaMessage.mockResolvedValue(Buffer.from('fake-image-bytes'));
      const adapter = new BaileysAdapter({ sessionId: 'test', authDir: tmpDir });
      const onMessage = jest.fn();
      await adapter.initialize({ onMessage });

      handlers['messages.upsert']({
        type: 'notify',
        messages: [
          {
            key: { remoteJid: '254711223344@s.whatsapp.net', id: 'wa-3', fromMe: false },
            message: { imageMessage: { mimetype: 'image/jpeg', caption: 'check this' } },
            messageTimestamp: 1782300002,
          },
        ],
      });
      await new Promise(process.nextTick);
      await new Promise(process.nextTick);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'image',
          body: 'check this',
          chatId: '254711223344@c.us',
          media: { mimetype: 'image/jpeg', data: Buffer.from('fake-image-bytes').toString('base64') },
        }),
      );
    });

    it('logs and continues (does not throw, does not attach media) when media download fails', async () => {
      const { handlers } = setupMockSock();
      mockDownloadMediaMessage.mockRejectedValue(new Error('expired media'));
      const adapter = new BaileysAdapter({ sessionId: 'test', authDir: tmpDir });
      const onMessage = jest.fn();
      await adapter.initialize({ onMessage });

      handlers['messages.upsert']({
        type: 'notify',
        messages: [
          {
            key: { remoteJid: '254711223344@s.whatsapp.net', id: 'wa-4', fromMe: false },
            message: { imageMessage: { mimetype: 'image/jpeg' } },
            messageTimestamp: 1782300003,
          },
        ],
      });
      await new Promise(process.nextTick);
      await new Promise(process.nextTick);

      expect(onMessage).toHaveBeenCalledWith(expect.objectContaining({ type: 'image', media: undefined }));
    });
  });

  describe('messages.reaction handling', () => {
    it('emits an IncomingReaction with chatId/senderId from the outer key and target info from reaction.key', async () => {
      const { handlers } = setupMockSock();
      const adapter = new BaileysAdapter({ sessionId: 'test', authDir: tmpDir });
      const onMessageReaction = jest.fn();
      await adapter.initialize({ onMessageReaction });

      handlers['messages.reaction']([
        {
          key: { remoteJid: '123@g.us', participant: '254700000001@s.whatsapp.net' },
          reaction: {
            key: { id: 'wa-target-1', remoteJid: '123@g.us', participant: '254711223344@s.whatsapp.net' },
            text: '👍',
          },
        },
      ]);

      expect(onMessageReaction).toHaveBeenCalledWith({
        chatId: '123@g.us',
        emoji: '👍',
        senderId: '254700000001@c.us',
        targetMessageId: 'wa-target-1',
        targetAuthor: '254711223344@c.us',
        targetTimestamp: undefined,
      });
    });
  });

  describe('messages.update handling (ack)', () => {
    it('calls onMessageAck with the message id and numeric status', async () => {
      const { handlers } = setupMockSock();
      const adapter = new BaileysAdapter({ sessionId: 'test', authDir: tmpDir });
      const onMessageAck = jest.fn();
      await adapter.initialize({ onMessageAck });

      handlers['messages.update']([{ key: { id: 'wa-1' }, update: { status: 4 } }]);

      expect(onMessageAck).toHaveBeenCalledWith('wa-1', 4);
    });
  });

  describe('disconnect / logout / destroy', () => {
    it('disconnect() ends the socket and sets status to DISCONNECTED without deleting auth files', async () => {
      const { mockSock } = setupMockSock();
      const adapter = new BaileysAdapter({ sessionId: 'dunhill', authDir: tmpDir });
      await adapter.initialize({});

      await adapter.disconnect();

      expect(mockSock.end).toHaveBeenCalledWith(undefined);
      expect(adapter.getStatus()).toBe(EngineStatus.DISCONNECTED);
      expect(fs.existsSync(path.join(tmpDir, 'dunhill'))).toBe(true);
    });

    it('logout() logs out, deletes the auth directory, and sets status to DISCONNECTED', async () => {
      const { mockSock } = setupMockSock();
      const adapter = new BaileysAdapter({ sessionId: 'dunhill', authDir: tmpDir });
      await adapter.initialize({});

      await adapter.logout();

      expect(mockSock.logout).toHaveBeenCalled();
      expect(fs.existsSync(path.join(tmpDir, 'dunhill'))).toBe(false);
      expect(adapter.getStatus()).toBe(EngineStatus.DISCONNECTED);
    });

    it('destroy() removes all listeners and ends the socket', async () => {
      const { mockSock } = setupMockSock();
      const adapter = new BaileysAdapter({ sessionId: 'dunhill', authDir: tmpDir });
      await adapter.initialize({});

      await adapter.destroy();

      expect(mockSock.ev.removeAllListeners).toHaveBeenCalledWith('connection.update');
      expect(mockSock.end).toHaveBeenCalledWith(undefined);
    });
  });

  describe('sendTextMessage', () => {
    it('converts the chatId to a Baileys JID and returns the sent message id/timestamp', async () => {
      const { mockSock } = setupMockSock();
      mockSock.sendMessage = jest.fn().mockResolvedValue({ key: { id: 'wa-sent-1' }, messageTimestamp: 1782300010 });
      const adapter = new BaileysAdapter({ sessionId: 'test', authDir: tmpDir });
      await adapter.initialize({});
      handlers_forceReady(adapter, mockSock);

      const result = await adapter.sendTextMessage('254711223344@c.us', 'Hello');

      expect(mockSock.sendMessage).toHaveBeenCalledWith('254711223344@s.whatsapp.net', { text: 'Hello' });
      expect(result).toEqual({ id: 'wa-sent-1', timestamp: 1782300010 });
    });

    it('leaves a group chatId (@g.us) unchanged when sending', async () => {
      const { mockSock } = setupMockSock();
      mockSock.sendMessage = jest.fn().mockResolvedValue({ key: { id: 'wa-sent-2' }, messageTimestamp: 1782300011 });
      const adapter = new BaileysAdapter({ sessionId: 'test', authDir: tmpDir });
      await adapter.initialize({});
      handlers_forceReady(adapter, mockSock);

      await adapter.sendTextMessage('123@g.us', 'Hello group');

      expect(mockSock.sendMessage).toHaveBeenCalledWith('123@g.us', { text: 'Hello group' });
    });
  });

  describe('replyToMessage', () => {
    async function setupReady() {
      const { mockSock } = setupMockSock();
      mockSock.sendMessage = jest.fn().mockResolvedValue({ key: { id: 'reply-1' }, messageTimestamp: 1000 });
      const adapter = new BaileysAdapter({ sessionId: 'test', authDir: tmpDir });
      const onMessage = jest.fn();
      await adapter.initialize({ onMessage });
      handlers_forceReady(adapter, mockSock);
      return { adapter, mockSock, handlers: lastHandlers };
    }

    it('replies by exact message ID when found in the store (unchanged behavior)', async () => {
      const { adapter, mockSock, handlers } = await setupReady();
      handlers['messages.upsert']({
        type: 'notify',
        messages: [
          {
            key: { remoteJid: '123@g.us', participant: '254711223344@s.whatsapp.net', id: 'wa-quoted-1', fromMe: false },
            message: { conversation: 'original' },
            messageTimestamp: 1700000000,
          },
        ],
      });
      await new Promise(process.nextTick);

      const result = await adapter.replyToMessage('123@g.us', 'wa-quoted-1', 'Hello');

      expect(mockSock.sendMessage).toHaveBeenCalledWith(
        '123@g.us',
        { text: 'Hello' },
        { quoted: expect.objectContaining({ key: expect.objectContaining({ id: 'wa-quoted-1' }) }) },
      );
      expect(result).toEqual({ id: 'reply-1', timestamp: 1000 });
    });

    it('falls back to author+timestamp match when exact ID is absent from the store', async () => {
      const { adapter, mockSock, handlers } = await setupReady();
      handlers['messages.upsert']({
        type: 'notify',
        messages: [
          {
            key: { remoteJid: '123@g.us', participant: '254711223344@s.whatsapp.net', id: 'some-other-id', fromMe: false },
            message: { conversation: 'original' },
            messageTimestamp: 1700000000,
          },
        ],
      });
      await new Promise(process.nextTick);

      const result = await adapter.replyToMessage(
        '123@g.us',
        'wa-quoted-missing',
        'Hello',
        '254711223344',
        1700000000,
        'Original snippet',
      );

      expect(mockSock.sendMessage).toHaveBeenCalledWith(
        '123@g.us',
        { text: 'Hello' },
        { quoted: expect.objectContaining({ key: expect.objectContaining({ id: 'some-other-id' }) }) },
      );
      expect(result).toEqual({ id: 'reply-1', timestamp: 1000 });
    });

    it('sends a plain message prefixed with the snippet when no match is found but hints were supplied', async () => {
      const { adapter, mockSock } = await setupReady();

      const result = await adapter.replyToMessage(
        '123@g.us',
        'wa-quoted-missing',
        'Hello',
        '254711223344',
        1700000000,
        'Original snippet',
      );

      expect(mockSock.sendMessage).toHaveBeenCalledWith('123@g.us', { text: '> Original snippet\n\nHello' });
      expect(result).toEqual({ id: 'reply-1', timestamp: 1000 });
    });

    it('throws when no hints are supplied at all and no exact match is found', async () => {
      const { adapter } = await setupReady();

      await expect(adapter.replyToMessage('123@g.us', 'wa-quoted-missing', 'Hello')).rejects.toThrow(
        'Message wa-quoted-missing not found',
      );
    });
  });

  describe('getGroups', () => {
    it('maps GroupMetadata to Group, resolving isAdmin from the own participant entry (via phoneNumber, not @lid id)', async () => {
      const { mockSock } = setupMockSock();
      mockSock.user = { id: 'OWNLID@lid', phoneNumber: '254700000000@s.whatsapp.net' };
      mockSock.groupFetchAllParticipating = jest.fn().mockResolvedValue({
        '123@g.us': {
          id: '123@g.us',
          subject: 'Dunhill Ops',
          participants: [
            { id: 'OWNLID@lid', phoneNumber: '254700000000@s.whatsapp.net', isAdmin: true },
            { id: '254711223344@s.whatsapp.net', isAdmin: false },
          ],
        },
      });
      const adapter = new BaileysAdapter({ sessionId: 'test', authDir: tmpDir });
      await adapter.initialize({});
      handlers_forceReady(adapter, mockSock);

      const groups = await adapter.getGroups();

      expect(groups).toEqual([
        { id: '123@g.us', name: 'Dunhill Ops', participantsCount: 2, isAdmin: true },
      ]);
    });
  });
});

/* eslint-enable @typescript-eslint/no-unsafe-member-access, @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-assignment, @typescript-eslint/no-explicit-any */
