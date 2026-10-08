import * as fs from 'fs';
import * as path from 'path';
import * as qrcode from 'qrcode';
import makeWASocket, {
  useMultiFileAuthState,
  downloadMediaMessage,
  fetchLatestWaWebVersion,
} from '@whiskeysockets/baileys';
import type { WASocket, WAMessage, WAMessageKey, WAMediaUpload } from '@whiskeysockets/baileys';
import {
  IWhatsAppEngine,
  EngineStatus,
  EngineEventCallbacks,
  MessageResult,
  MediaInput,
  IncomingMessage,
  HistoryQuery,
  IncomingReaction,
  Contact,
  Group,
  GroupInfo,
  LocationInput,
  ContactCard,
  MessageReaction,
  Label,
  Channel,
  ChannelMessage,
  Status,
  TextStatusOptions,
  StatusResult,
  Catalog,
  Product,
  ProductQueryOptions,
  PaginatedProducts,
} from '../interfaces/whatsapp-engine.interface';
import { createLogger } from '../../common/services/logger.service';
import {
  resolveParticipantJid,
  resolveRemoteJid,
  resolveContactJid,
  toBaileysJid,
  mapBaileysMessageType,
} from './baileys-jid.util';
import { BaileysSessionStore } from './baileys-session-store';
import { BaileysContactDirectory } from './baileys-contact-directory';

export interface BaileysAdapterConfig {
  sessionId: string;
  authDir: string;
  // Ask the phone for its FULL chat history at link time (one-off historical
  // backfills). Off by default: the normal sync only covers recent messages.
  fullHistory?: boolean;
}

function timestampToNumber(ts: unknown): number {
  if (typeof ts === 'number') {
    return ts;
  }
  if (ts && typeof (ts as { toNumber?: () => number }).toNumber === 'function') {
    return (ts as { toNumber: () => number }).toNumber();
  }
  return Math.floor(Date.now() / 1000);
}

// MediaInput.data is a Buffer, a base64 string, or a URL string (the same
// three forms message.service.ts's buildMediaInput produces for every
// engine) -- Baileys' WAMediaUpload wants a Buffer or a { url } payload,
// so a base64 string needs decoding first.
function toBaileysMediaUpload(media: { data: Buffer | string }): WAMediaUpload {
  if (Buffer.isBuffer(media.data)) {
    return media.data;
  }
  if (media.data.startsWith('http://') || media.data.startsWith('https://')) {
    return { url: media.data };
  }
  return Buffer.from(media.data, 'base64');
}

// Baileys DisconnectReason codes. Inlined rather than imported so the
// module stays loadable where '@whiskeysockets/baileys' is jest-mocked.
const BAILEYS_LOGGED_OUT = 401;
const BAILEYS_RESTART_REQUIRED = 515;

export class BaileysAdapter implements IWhatsAppEngine {
  private sock: WASocket | null = null;
  private status: EngineStatus = EngineStatus.DISCONNECTED;
  private qrCode: string | null = null;
  private phoneNumber: string | null = null;
  private pushName: string | null = null;
  private callbacks: EngineEventCallbacks = {};
  private readonly store = new BaileysSessionStore();
  private readonly contacts: BaileysContactDirectory;
  private readonly logger = createLogger('BaileysAdapter');

  constructor(private readonly config: BaileysAdapterConfig) {
    // Outside <authDir>/<sessionId> (wiped on re-link), like the history file.
    this.contacts = new BaileysContactDirectory(path.join(config.authDir, 'contacts', `${config.sessionId}.json`));
  }

  async initialize(callbacks: EngineEventCallbacks): Promise<void> {
    this.callbacks = callbacks;

    try {
      const authPath = path.join(this.config.authDir, this.config.sessionId);
      fs.mkdirSync(authPath, { recursive: true });

      const { state, saveCreds } = await useMultiFileAuthState(authPath);

      // Baileys' bundled default WA Web version goes stale and WhatsApp's
      // servers reject it outright ("Connection Failure" immediately after
      // "attempting registration...", before a QR is ever issued -- see
      // WhiskeySockets/Baileys#2679). Fetch the actual current version at
      // connect time instead of trusting the library's bundled default. If
      // the fetch itself fails (e.g. no outbound network to the version-check
      // endpoint), fall back to the bundled default rather than blocking
      // startup entirely.
      //
      // Baileys is pinned to 6.x: since 2026-10 WhatsApp terminates new-device
      // registration from 7.0.0-rc13/rc14 with 428 before a QR is issued.
      const { version } = await fetchLatestWaWebVersion({}).catch(error => {
        this.logger.error('Failed to fetch latest WA Web version, using bundled default', String(error));
        return { version: undefined };
      });

      this.sock = makeWASocket({
        auth: state,
        ...(version ? { version } : {}),
        // Always keep the link-time history sync (saved for backfills). Baileys
        // 6.x otherwise drops every history chunk unless syncFullHistory is on.
        shouldSyncHistoryMessage: () => true,
        ...(this.config.fullHistory
          ? {
              syncFullHistory: true,
              // WhatsApp only sends full history to desktop-class companions.
              // Literal tuple rather than Browsers.macOS() so the module stays
              // loadable where '@whiskeysockets/baileys' is jest-mocked.
              browser: ['Mac OS', 'Desktop', '14.4.1'] as [string, string, string],
            }
          : {}),
      });

      this.setupEventHandlers(saveCreds);
    } catch (error) {
      this.setStatus(EngineStatus.FAILED);
      throw error;
    }
  }

  private setupEventHandlers(saveCreds: () => Promise<void>): void {
    if (!this.sock) return;

    this.sock.ev.on('creds.update', () => {
      void saveCreds();
    });

    this.sock.ev.on('connection.update', update => {
      if (update.qr) {
        void this.handleQrCode(update.qr);
      }

      if (update.connection === 'open') {
        this.handleConnectionOpen();
      }

      if (update.connection === 'close') {
        const error = update.lastDisconnect?.error as (Error & { output?: { statusCode?: number } }) | undefined;
        const statusCode = error?.output?.statusCode;
        const reason = error?.message || 'Connection closed';
        this.setStatus(EngineStatus.DISCONNECTED);
        this.callbacks.onDisconnected?.(statusCode ? `${reason} (${statusCode})` : reason, {
          loggedOut: statusCode === BAILEYS_LOGGED_OUT,
          restartRequired: statusCode === BAILEYS_RESTART_REQUIRED,
        });
      }
    });

    this.sock.ev.on('messages.upsert', ({ messages }) => {
      for (const msg of messages) {
        void this.handleIncomingMessage(msg);
      }
    });

    // The phone pushes recent chat history once, right after a device is
    // linked. It is never dispatched live (old messages must not trigger
    // auto-replies); it is kept on disk for one-off backfills via getHistory().
    this.sock.ev.on('messaging-history.set', ({ contacts, messages, syncType, isLatest }) => {
      // Contacts first: they carry the lid -> phone mappings and names the
      // messages in the same chunk need.
      for (const contact of contacts ?? []) {
        this.contacts.learnContact(contact);
      }
      void this.saveHistory(messages, syncType, isLatest);
    });

    this.sock.ev.on('contacts.upsert', contacts => {
      for (const contact of contacts) {
        this.contacts.learnContact(contact);
      }
    });

    this.sock.ev.on('contacts.update', updates => {
      for (const contact of updates) {
        this.contacts.learnContact(contact);
      }
    });

    this.sock.ev.on('chats.phoneNumberShare', ({ lid, jid }) => {
      this.contacts.learnContact({ lid, jid });
    });

    this.sock.ev.on('groups.upsert', groups => {
      for (const group of groups) {
        for (const participant of group.participants ?? []) {
          this.contacts.learnContact(participant);
        }
      }
    });

    this.sock.ev.on('messages.reaction', reactions => {
      for (const reaction of reactions) {
        this.handleReaction(reaction);
      }
    });

    this.sock.ev.on('messages.update', updates => {
      for (const { key, update } of updates) {
        if (key.id && typeof update.status === 'number') {
          this.callbacks.onMessageAck?.(key.id, update.status);
        }
      }
    });
  }

  private async handleQrCode(qr: string): Promise<void> {
    try {
      this.qrCode = await qrcode.toDataURL(qr);
      this.setStatus(EngineStatus.QR_READY);
      this.callbacks.onQRCode?.(this.qrCode);
    } catch (error) {
      this.logger.error('Error generating QR code', String(error));
    }
  }

  private handleConnectionOpen(): void {
    const user = this.sock?.user;
    // Contact.id may be an "@lid" (opaque linked-device identifier); prefer
    // the phone-number JID so our own number always resolves to an actual
    // phone number, not an opaque lid.
    const ownJid = user ? resolveContactJid(user) : '';
    this.phoneNumber = ownJid ? ownJid.split('@')[0] : null;
    this.pushName = user?.notify || user?.name || null;
    this.qrCode = null;
    this.setStatus(EngineStatus.READY);
    this.callbacks.onReady?.(this.phoneNumber || '', this.pushName || '');
    void this.learnGroupParticipants();
  }

  /** Group participant lists carry the lid <-> phone mapping for every member. */
  private async learnGroupParticipants(): Promise<void> {
    try {
      const groups = await this.sock!.groupFetchAllParticipating();
      for (const meta of Object.values(groups)) {
        for (const participant of meta.participants) {
          this.contacts.learnContact(participant);
        }
      }
    } catch (error) {
      this.logger.warn('Could not fetch group participants for the contact directory', String(error));
    }
  }

  private async handleIncomingMessage(msg: WAMessage): Promise<void> {
    // sock.sendMessage() (used by sendTextMessage/replyToMessage) internally
    // upserts its own outgoing message, re-emitting it here with
    // key.fromMe: true. whatsapp-web.js's Client never emits its own sends as
    // an inbound 'message' event, and the backend's ingest pipeline relies on
    // that — without this guard, every dashboard-sent reply loops back in as
    // a brand-new incoming message (and can self-trigger the DM auto-reply).
    if (msg.key.fromMe) {
      return;
    }

    try {
      const incomingMessage = this.toIncomingMessage(msg);
      const { id, chatId, author, timestamp, type } = incomingMessage;

      this.store.add({ id, chatId, author, timestamp, raw: msg });

      if (type !== 'chat' && type !== 'unknown') {
        try {
          const buffer = await downloadMediaMessage(msg, 'buffer', {});
          incomingMessage.media = { ...this.extractMediaMeta(msg), data: buffer.toString('base64') };
        } catch (error) {
          this.logger.error('Error downloading media', String(error));
        }
      }

      this.callbacks.onMessage?.(incomingMessage);
    } catch (error) {
      this.logger.error('Error processing incoming message', String(error));
    }
  }

  /** Normalizes a Baileys message to the engine-neutral shape (no media download). */
  private toIncomingMessage(msg: WAMessage): IncomingMessage {
    this.learnFromKey(msg.key);
    const chatId = this.contacts.resolve(resolveRemoteJid(msg.key)) || '';
    const isGroup = chatId.endsWith('@g.us');
    // History-sync group messages carry the sender on WebMessageInfo.participant
    // rather than key.participant; live messages use the key.
    const author = this.contacts.resolve(
      resolveParticipantJid(msg.key) ||
        (msg.participant ? resolveParticipantJid({ participant: msg.participant }) : undefined),
    );
    this.contacts.learnPushName(isGroup ? author : chatId, msg.pushName);
    const incomingMessage: IncomingMessage = {
      id: msg.key.id || '',
      from: chatId,
      // For a group message "to" is the group itself; for a DM it's this
      // session's own number (matching whatsapp-web.js's msg.to semantics).
      to: isGroup ? chatId : this.phoneNumber ? `${this.phoneNumber}@c.us` : '',
      chatId,
      body: this.extractBody(msg),
      // proto.IMessage (the real installed type of msg.message) has no index
      // signature, unlike the Record<string, unknown> that mapBaileysMessageType
      // (baileys-jid.util, Task 2) declares for its parameter -- cast at the
      // call site rather than touching that already-completed file.
      type: mapBaileysMessageType(msg.message as Record<string, unknown> | null | undefined),
      timestamp: timestampToNumber(msg.messageTimestamp),
      fromMe: msg.key.fromMe || false,
      isGroup,
      author,
      notifyName: msg.pushName || this.contacts.nameFor(isGroup ? author : chatId),
      media: undefined,
    };
    const quoted = this.extractQuotedMessage(msg);
    if (quoted) {
      incomingMessage.quotedMessage = quoted;
    }
    return incomingMessage;
  }

  private historyPath(): string {
    // Deliberately outside <authDir>/<sessionId>, which clearAuthState() wipes
    // on every re-link -- exactly when the history sync arrives.
    return path.join(this.config.authDir, 'history', `${this.config.sessionId}.jsonl`);
  }

  private async saveHistory(messages: WAMessage[], syncType: unknown, isLatest: boolean | undefined): Promise<void> {
    try {
      const lines = messages
        .filter(msg => msg.key?.id && !msg.key.fromMe && msg.message)
        .map(msg => JSON.stringify(this.toIncomingMessage(msg)) + '\n');
      this.logger.log(
        `History sync: ${messages.length} messages, ${lines.length} kept (syncType=${String(syncType)}, isLatest=${String(isLatest)})`,
      );
      if (lines.length === 0) return;
      await fs.promises.mkdir(path.dirname(this.historyPath()), { recursive: true });
      await fs.promises.appendFile(this.historyPath(), lines.join(''));
    } catch (error) {
      this.logger.error('Failed to save history sync', String(error));
    }
  }

  async getHistory(opts: HistoryQuery = {}): Promise<IncomingMessage[]> {
    let raw: string;
    try {
      raw = await fs.promises.readFile(this.historyPath(), 'utf8');
    } catch {
      return [];
    }
    // Later syncs can resend a message; keep one copy per id.
    const byId = new Map<string, IncomingMessage>();
    for (const line of raw.split('\n')) {
      if (!line.trim()) continue;
      try {
        const msg = JSON.parse(line) as IncomingMessage;
        byId.set(msg.id, msg);
      } catch {
        // a torn final line from a crash mid-append -- skip it
      }
    }
    // Resolve again on read: mappings learned after a message was saved
    // (e.g. from a later history chunk) still apply.
    return [...byId.values()]
      .map(msg => this.withResolvedSender(msg))
      .filter(
        msg =>
          (!opts.chatId || msg.chatId === opts.chatId) &&
          (opts.since === undefined || msg.timestamp >= opts.since) &&
          (opts.until === undefined || msg.timestamp <= opts.until),
      )
      .sort((a, b) => a.timestamp - b.timestamp);
  }

  private withResolvedSender(msg: IncomingMessage): IncomingMessage {
    const chatId = this.contacts.resolve(msg.chatId) || msg.chatId;
    const author = this.contacts.resolve(msg.author);
    const sender = msg.isGroup ? author : chatId;
    return {
      ...msg,
      chatId,
      from: msg.isGroup ? msg.from : chatId,
      author,
      notifyName: msg.notifyName || this.contacts.nameFor(sender),
    };
  }

  /** Baileys 6.x keys carry the phone number next to an @lid when WhatsApp disclosed it. */
  private learnFromKey(key: WAMessageKey): void {
    if (key.participant?.endsWith('@lid') && key.participantPn) {
      this.contacts.learnContact({ lid: key.participant, jid: key.participantPn });
    } else if (key.participantLid && key.participant) {
      this.contacts.learnContact({ lid: key.participantLid, jid: key.participant });
    }
    if (key.remoteJid?.endsWith('@lid') && key.senderPn) {
      this.contacts.learnContact({ lid: key.remoteJid, jid: key.senderPn });
    } else if (key.senderLid && key.remoteJid?.endsWith('@s.whatsapp.net')) {
      this.contacts.learnContact({ lid: key.senderLid, jid: key.remoteJid });
    }
  }

  private extractBody(msg: WAMessage): string {
    const content = msg.message;
    return (
      content?.conversation ||
      content?.extendedTextMessage?.text ||
      content?.imageMessage?.caption ||
      content?.videoMessage?.caption ||
      content?.documentMessage?.caption ||
      ''
    );
  }

  private extractMediaMeta(msg: WAMessage): { mimetype: string; filename?: string } {
    const content = msg.message;
    if (content?.imageMessage) {
      return { mimetype: content.imageMessage.mimetype || 'image/jpeg' };
    }
    if (content?.videoMessage) {
      return { mimetype: content.videoMessage.mimetype || 'video/mp4' };
    }
    if (content?.audioMessage) {
      return { mimetype: content.audioMessage.mimetype || 'audio/ogg' };
    }
    if (content?.documentMessage) {
      return {
        mimetype: content.documentMessage.mimetype || 'application/octet-stream',
        filename: content.documentMessage.fileName || undefined,
      };
    }
    return { mimetype: 'application/octet-stream' };
  }

  // Only reads contextInfo off extendedTextMessage; Baileys also carries it on
  // imageMessage/videoMessage/documentMessage, so a quote-reply to a media
  // message currently loses its quotedMessage link. Not extended to those yet
  // because IncomingMessage.quotedMessage has no consumer today (Phase 1
  // scope) — revisit if/when something reads it.
  private extractQuotedMessage(msg: WAMessage): { id: string; body: string } | undefined {
    const contextInfo = msg.message?.extendedTextMessage?.contextInfo;
    if (!contextInfo?.quotedMessage || !contextInfo.stanzaId) {
      return undefined;
    }
    return {
      id: contextInfo.stanzaId,
      body: contextInfo.quotedMessage.conversation || contextInfo.quotedMessage.extendedTextMessage?.text || '',
    };
  }

  private handleReaction(reaction: {
    key: WAMessageKey;
    reaction: { key?: WAMessageKey | null; text?: string | null };
  }): void {
    const chatId = this.contacts.resolve(resolveRemoteJid(reaction.key)) || '';
    const senderId = this.contacts.resolve(resolveParticipantJid(reaction.key)) || chatId;
    const targetKey = reaction.reaction.key;
    const stored = targetKey?.id ? this.store.findById(chatId, targetKey.id) : undefined;

    const incomingReaction: IncomingReaction = {
      chatId,
      emoji: reaction.reaction.text || '',
      senderId,
      targetMessageId: targetKey?.id || undefined,
      targetAuthor: targetKey ? this.contacts.resolve(resolveParticipantJid(targetKey)) : undefined,
      targetTimestamp: stored?.timestamp,
    };
    this.callbacks.onMessageReaction?.(incomingReaction);
  }

  private setStatus(status: EngineStatus): void {
    this.status = status;
    this.callbacks.onStateChanged?.(status);
  }

  private ensureReady(): void {
    if (this.status !== EngineStatus.READY || !this.sock) {
      throw new Error('Baileys client is not ready');
    }
  }

  disconnect(): Promise<void> {
    this.sock?.end(undefined);
    this.setStatus(EngineStatus.DISCONNECTED);
    return Promise.resolve();
  }

  async logout(): Promise<void> {
    try {
      await this.sock?.logout();
    } catch (error) {
      // Not connected / already logged out -- still wipe the local auth below.
      this.logger.warn('Logout failed:', String(error));
    }
    await this.clearAuthState();
    this.sock = null;
    this.setStatus(EngineStatus.DISCONNECTED);
  }

  async clearAuthState(): Promise<void> {
    await fs.promises.rm(path.join(this.config.authDir, this.config.sessionId), { recursive: true, force: true });
  }

  destroy(): Promise<void> {
    this.sock?.ev.removeAllListeners('connection.update');
    this.sock?.ev.removeAllListeners('creds.update');
    this.sock?.ev.removeAllListeners('messages.upsert');
    this.sock?.ev.removeAllListeners('messaging-history.set');
    this.sock?.ev.removeAllListeners('messages.reaction');
    this.sock?.ev.removeAllListeners('messages.update');
    this.sock?.ev.removeAllListeners('contacts.upsert');
    this.sock?.ev.removeAllListeners('contacts.update');
    this.sock?.ev.removeAllListeners('chats.phoneNumberShare');
    this.sock?.ev.removeAllListeners('groups.upsert');
    void this.contacts.save().catch(error => this.logger.warn('Saving contact directory failed:', String(error)));
    try {
      this.sock?.end(undefined);
    } catch (error) {
      this.logger.warn('Socket end failed during destroy:', String(error));
    }
    this.sock = null;
    return Promise.resolve();
  }

  getStatus(): EngineStatus {
    return this.status;
  }

  getQRCode(): string | null {
    return this.qrCode;
  }

  getPhoneNumber(): string | null {
    return this.phoneNumber;
  }

  getPushName(): string | null {
    return this.pushName;
  }

  // ========== Messaging - Basic ==========

  async sendTextMessage(chatId: string, text: string): Promise<MessageResult> {
    this.ensureReady();
    const result = await this.sock!.sendMessage(toBaileysJid(chatId), { text });
    if (!result?.key.id) {
      throw new Error('sendTextMessage failed: no message returned from Baileys');
    }
    return { id: result.key.id, timestamp: timestampToNumber(result.messageTimestamp) };
  }

  async replyToMessage(
    chatId: string,
    quotedMsgId: string,
    text: string,
    authorHint?: string,
    timestampHint?: number,
    contextSnippet?: string,
  ): Promise<MessageResult> {
    this.ensureReady();
    const jid = toBaileysJid(chatId);

    let quoted = this.store.findById(chatId, quotedMsgId);

    if (!quoted && authorHint && timestampHint) {
      quoted = this.store.findByAuthorAndTimestamp(chatId, authorHint, timestampHint);
    }

    if (!quoted) {
      if (!authorHint && !timestampHint) {
        throw new Error(`Message ${quotedMsgId} not found`);
      }
      const body = contextSnippet ? `> ${contextSnippet}\n\n${text}` : text;
      const result = await this.sock!.sendMessage(jid, { text: body });
      if (!result?.key.id) {
        throw new Error('replyToMessage failed: no message returned from Baileys');
      }
      return { id: result.key.id, timestamp: timestampToNumber(result.messageTimestamp) };
    }

    const result = await this.sock!.sendMessage(jid, { text }, { quoted: quoted.raw });
    if (!result?.key.id) {
      throw new Error('replyToMessage failed: no message returned from Baileys');
    }
    return { id: result.key.id, timestamp: timestampToNumber(result.messageTimestamp) };
  }

  async getGroups(): Promise<Group[]> {
    this.ensureReady();
    const groupsMeta = await this.sock!.groupFetchAllParticipating();
    const ownJid = this.sock!.user ? resolveContactJid(this.sock!.user) : undefined;

    return Object.values(groupsMeta).map(meta => {
      meta.participants.forEach(participant => this.contacts.learnContact(participant));
      const ownParticipant = meta.participants.find(p => resolveContactJid(p) === ownJid);
      return {
        id: meta.id,
        name: meta.subject,
        participantsCount: meta.participants.length,
        isAdmin: ownParticipant ? Boolean(ownParticipant.isAdmin || ownParticipant.isSuperAdmin) : false,
      };
    });
  }

  // Added despite the Phase-1 media-send exclusion (design doc §4/§9): billing's
  // /statement command depends on sending a PDF, and Baileys throwing here was
  // failing that command completely silently (billing/whatsapp.py's send call
  // doesn't check the response status, so the error never surfaces to the user).
  async sendDocumentMessage(chatId: string, media: MediaInput): Promise<MessageResult> {
    this.ensureReady();
    const jid = toBaileysJid(chatId);
    const result = await this.sock!.sendMessage(jid, {
      document: toBaileysMediaUpload(media),
      mimetype: media.mimetype,
      fileName: media.filename,
      caption: media.caption,
    });
    if (!result?.key.id) {
      throw new Error('sendDocumentMessage failed: no message returned from Baileys');
    }
    return { id: result.key.id, timestamp: timestampToNumber(result.messageTimestamp) };
  }

  // ========== Everything below is out of Phase-1 scope (see design doc §4/§9) ==========
  /* eslint-disable @typescript-eslint/require-await, @typescript-eslint/no-unused-vars */

  async sendImageMessage(_chatId: string, _media: MediaInput): Promise<MessageResult> {
    throw new Error('sendImageMessage not yet implemented in baileys adapter');
  }

  async sendVideoMessage(_chatId: string, _media: MediaInput): Promise<MessageResult> {
    throw new Error('sendVideoMessage not yet implemented in baileys adapter');
  }

  async sendAudioMessage(_chatId: string, _media: MediaInput): Promise<MessageResult> {
    throw new Error('sendAudioMessage not yet implemented in baileys adapter');
  }

  async sendLocationMessage(_chatId: string, _location: LocationInput): Promise<MessageResult> {
    throw new Error('sendLocationMessage not yet implemented in baileys adapter');
  }

  async sendContactMessage(_chatId: string, _contact: ContactCard): Promise<MessageResult> {
    throw new Error('sendContactMessage not yet implemented in baileys adapter');
  }

  async sendStickerMessage(_chatId: string, _media: MediaInput): Promise<MessageResult> {
    throw new Error('sendStickerMessage not yet implemented in baileys adapter');
  }

  async forwardMessage(_fromChatId: string, _toChatId: string, _messageId: string): Promise<MessageResult> {
    throw new Error('forwardMessage not yet implemented in baileys adapter');
  }

  async reactToMessage(_chatId: string, _messageId: string, _emoji: string): Promise<void> {
    throw new Error('reactToMessage not yet implemented in baileys adapter');
  }

  async getMessageReactions(_chatId: string, _messageId: string): Promise<MessageReaction[]> {
    throw new Error('getMessageReactions not yet implemented in baileys adapter');
  }

  async getContacts(): Promise<Contact[]> {
    return this.contacts.list().map(entry => ({
      id: entry.id,
      name: entry.savedName || entry.verifiedName,
      pushName: entry.pushName,
      number: entry.id.split('@')[0],
      isMyContact: Boolean(entry.savedName),
      isBlocked: false,
    }));
  }

  async getContactById(_contactId: string): Promise<Contact | null> {
    this.logger.warn('getContactById not implemented in baileys adapter');
    return null;
  }

  async checkNumberExists(_number: string): Promise<boolean> {
    throw new Error('checkNumberExists not yet implemented in baileys adapter');
  }

  async getGroupInfo(_groupId: string): Promise<GroupInfo | null> {
    this.logger.warn('getGroupInfo not implemented in baileys adapter');
    return null;
  }

  async createGroup(_name: string, _participants: string[]): Promise<Group> {
    throw new Error('createGroup not yet implemented in baileys adapter');
  }

  async addParticipants(_groupId: string, _participants: string[]): Promise<void> {
    throw new Error('addParticipants not yet implemented in baileys adapter');
  }

  async removeParticipants(_groupId: string, _participants: string[]): Promise<void> {
    throw new Error('removeParticipants not yet implemented in baileys adapter');
  }

  async promoteParticipants(_groupId: string, _participants: string[]): Promise<void> {
    throw new Error('promoteParticipants not yet implemented in baileys adapter');
  }

  async demoteParticipants(_groupId: string, _participants: string[]): Promise<void> {
    throw new Error('demoteParticipants not yet implemented in baileys adapter');
  }

  async leaveGroup(_groupId: string): Promise<void> {
    throw new Error('leaveGroup not yet implemented in baileys adapter');
  }

  async setGroupSubject(_groupId: string, _subject: string): Promise<void> {
    throw new Error('setGroupSubject not yet implemented in baileys adapter');
  }

  async setGroupDescription(_groupId: string, _description: string): Promise<void> {
    throw new Error('setGroupDescription not yet implemented in baileys adapter');
  }

  async getGroupInviteCode(_groupId: string): Promise<string> {
    throw new Error('getGroupInviteCode not yet implemented in baileys adapter');
  }

  async revokeGroupInviteCode(_groupId: string): Promise<string> {
    throw new Error('revokeGroupInviteCode not yet implemented in baileys adapter');
  }

  async deleteMessage(_chatId: string, _messageId: string, _forEveryone?: boolean): Promise<void> {
    throw new Error('deleteMessage not yet implemented in baileys adapter');
  }

  async getProfilePicture(_contactId: string): Promise<string | null> {
    this.logger.warn('getProfilePicture not implemented in baileys adapter');
    return null;
  }

  async blockContact(_contactId: string): Promise<void> {
    throw new Error('blockContact not yet implemented in baileys adapter');
  }

  async unblockContact(_contactId: string): Promise<void> {
    throw new Error('unblockContact not yet implemented in baileys adapter');
  }

  async getLabels(): Promise<Label[]> {
    this.logger.warn('getLabels not implemented in baileys adapter');
    return [];
  }

  async getLabelById(_labelId: string): Promise<Label | null> {
    this.logger.warn('getLabelById not implemented in baileys adapter');
    return null;
  }

  async getChatLabels(_chatId: string): Promise<Label[]> {
    this.logger.warn('getChatLabels not implemented in baileys adapter');
    return [];
  }

  async addLabelToChat(_chatId: string, _labelId: string): Promise<void> {
    throw new Error('addLabelToChat not yet implemented in baileys adapter');
  }

  async removeLabelFromChat(_chatId: string, _labelId: string): Promise<void> {
    throw new Error('removeLabelFromChat not yet implemented in baileys adapter');
  }

  async getSubscribedChannels(): Promise<Channel[]> {
    this.logger.warn('getSubscribedChannels not implemented in baileys adapter');
    return [];
  }

  async getChannelById(_channelId: string): Promise<Channel | null> {
    this.logger.warn('getChannelById not implemented in baileys adapter');
    return null;
  }

  async subscribeToChannel(_inviteCode: string): Promise<Channel> {
    throw new Error('subscribeToChannel not yet implemented in baileys adapter');
  }

  async unsubscribeFromChannel(_channelId: string): Promise<void> {
    throw new Error('unsubscribeFromChannel not yet implemented in baileys adapter');
  }

  async getChannelMessages(_channelId: string, _limit?: number): Promise<ChannelMessage[]> {
    this.logger.warn('getChannelMessages not implemented in baileys adapter');
    return [];
  }

  async getContactStatuses(): Promise<Status[]> {
    this.logger.warn('getContactStatuses not implemented in baileys adapter');
    return [];
  }

  async getContactStatus(_contactId: string): Promise<Status[]> {
    this.logger.warn('getContactStatus not implemented in baileys adapter');
    return [];
  }

  async postTextStatus(_text: string, _options?: TextStatusOptions): Promise<StatusResult> {
    throw new Error('postTextStatus not yet implemented in baileys adapter');
  }

  async postImageStatus(_media: MediaInput, _caption?: string): Promise<StatusResult> {
    throw new Error('postImageStatus not yet implemented in baileys adapter');
  }

  async postVideoStatus(_media: MediaInput, _caption?: string): Promise<StatusResult> {
    throw new Error('postVideoStatus not yet implemented in baileys adapter');
  }

  async deleteStatus(_statusId: string): Promise<void> {
    throw new Error('deleteStatus not yet implemented in baileys adapter');
  }

  async getCatalog(): Promise<Catalog | null> {
    this.logger.warn('getCatalog not implemented in baileys adapter');
    return null;
  }

  async getProducts(_options?: ProductQueryOptions): Promise<PaginatedProducts> {
    this.logger.warn('getProducts not implemented in baileys adapter');
    return { products: [], pagination: { page: 1, limit: 20, total: 0, totalPages: 0 } };
  }

  async getProduct(_productId: string): Promise<Product | null> {
    this.logger.warn('getProduct not implemented in baileys adapter');
    return null;
  }

  async sendProduct(_chatId: string, _productId: string, _body?: string): Promise<MessageResult> {
    throw new Error('sendProduct not yet implemented in baileys adapter');
  }

  async sendCatalog(_chatId: string, _body?: string): Promise<MessageResult> {
    throw new Error('sendCatalog not yet implemented in baileys adapter');
  }

  /* eslint-enable @typescript-eslint/require-await, @typescript-eslint/no-unused-vars */
}
