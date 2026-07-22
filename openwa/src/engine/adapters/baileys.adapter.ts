import * as fs from 'fs';
import * as path from 'path';
import * as qrcode from 'qrcode';
import makeWASocket, { useMultiFileAuthState, downloadMediaMessage } from '@whiskeysockets/baileys';
import type { WASocket, WAMessage, WAMessageKey } from '@whiskeysockets/baileys';
import {
  IWhatsAppEngine,
  EngineStatus,
  EngineEventCallbacks,
  MessageResult,
  MediaInput,
  IncomingMessage,
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

export interface BaileysAdapterConfig {
  sessionId: string;
  authDir: string;
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

export class BaileysAdapter implements IWhatsAppEngine {
  private sock: WASocket | null = null;
  private status: EngineStatus = EngineStatus.DISCONNECTED;
  private qrCode: string | null = null;
  private phoneNumber: string | null = null;
  private pushName: string | null = null;
  private callbacks: EngineEventCallbacks = {};
  private readonly store = new BaileysSessionStore();
  private readonly logger = createLogger('BaileysAdapter');

  constructor(private readonly config: BaileysAdapterConfig) {}

  async initialize(callbacks: EngineEventCallbacks): Promise<void> {
    this.callbacks = callbacks;

    const authPath = path.join(this.config.authDir, this.config.sessionId);
    fs.mkdirSync(authPath, { recursive: true });

    const { state, saveCreds } = await useMultiFileAuthState(authPath);
    this.sock = makeWASocket({ auth: state });

    this.setupEventHandlers(saveCreds);
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
        const reason = update.lastDisconnect?.error?.message || 'Connection closed';
        this.setStatus(EngineStatus.DISCONNECTED);
        this.callbacks.onDisconnected?.(reason);
      }
    });

    this.sock.ev.on('messages.upsert', ({ messages }) => {
      for (const msg of messages) {
        void this.handleIncomingMessage(msg);
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
    // Baileys 7.x prefers "@lid" (opaque linked-device identifiers) as
    // Contact.id; Contact.phoneNumber carries the real @s.whatsapp.net
    // address when WhatsApp has disclosed it. Prefer phoneNumber so our own
    // number always resolves to an actual phone number, not an opaque lid.
    const ownJid = user ? resolveRemoteJid({ remoteJid: user.phoneNumber || user.id }) : '';
    this.phoneNumber = ownJid ? ownJid.split('@')[0] : null;
    this.pushName = user?.notify || user?.name || null;
    this.qrCode = null;
    this.setStatus(EngineStatus.READY);
    this.callbacks.onReady?.(this.phoneNumber || '', this.pushName || '');
  }

  private async handleIncomingMessage(msg: WAMessage): Promise<void> {
    try {
      const chatId = resolveRemoteJid(msg.key) || '';
      const author = resolveParticipantJid(msg.key);
      // proto.IMessage (the real installed type of msg.message) has no index
      // signature, unlike the Record<string, unknown> that mapBaileysMessageType
      // (baileys-jid.util, Task 2) declares for its parameter -- cast at the
      // call site rather than touching that already-completed file.
      const type = mapBaileysMessageType(msg.message as Record<string, unknown> | null | undefined);
      const timestamp = timestampToNumber(msg.messageTimestamp);
      const body = this.extractBody(msg);

      this.store.add({ id: msg.key.id || '', chatId, author, timestamp, raw: msg });

      const incomingMessage: IncomingMessage = {
        id: msg.key.id || '',
        from: chatId,
        to: this.phoneNumber ? `${this.phoneNumber}@c.us` : '',
        chatId,
        body,
        type,
        timestamp,
        fromMe: msg.key.fromMe || false,
        isGroup: chatId.endsWith('@g.us'),
        author,
        notifyName: msg.pushName || undefined,
        media: undefined,
      };

      if (type !== 'chat' && type !== 'unknown') {
        try {
          const buffer = await downloadMediaMessage(msg, 'buffer', {});
          incomingMessage.media = { ...this.extractMediaMeta(msg), data: buffer.toString('base64') };
        } catch (error) {
          this.logger.error('Error downloading media', String(error));
        }
      }

      const quoted = this.extractQuotedMessage(msg);
      if (quoted) {
        incomingMessage.quotedMessage = quoted;
      }

      this.callbacks.onMessage?.(incomingMessage);
    } catch (error) {
      this.logger.error('Error processing incoming message', String(error));
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
    const chatId = resolveRemoteJid(reaction.key) || '';
    const senderId = resolveParticipantJid(reaction.key) || chatId;
    const targetKey = reaction.reaction.key;
    const stored = targetKey?.id ? this.store.findById(chatId, targetKey.id) : undefined;

    const incomingReaction: IncomingReaction = {
      chatId,
      emoji: reaction.reaction.text || '',
      senderId,
      targetMessageId: targetKey?.id || undefined,
      targetAuthor: targetKey ? resolveParticipantJid(targetKey) : undefined,
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

  async disconnect(): Promise<void> {
    await this.sock?.end(undefined);
    this.setStatus(EngineStatus.DISCONNECTED);
  }

  async logout(): Promise<void> {
    await this.sock?.logout();
    const authPath = path.join(this.config.authDir, this.config.sessionId);
    fs.rmSync(authPath, { recursive: true, force: true });
    this.sock = null;
    this.setStatus(EngineStatus.DISCONNECTED);
  }

  async destroy(): Promise<void> {
    this.sock?.ev.removeAllListeners('connection.update');
    this.sock?.ev.removeAllListeners('creds.update');
    this.sock?.ev.removeAllListeners('messages.upsert');
    this.sock?.ev.removeAllListeners('messages.reaction');
    this.sock?.ev.removeAllListeners('messages.update');
    await this.sock?.end(undefined);
    this.sock = null;
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
      const ownParticipant = meta.participants.find(p => resolveContactJid(p) === ownJid);
      return {
        id: meta.id,
        name: meta.subject,
        participantsCount: meta.participants.length,
        isAdmin: ownParticipant ? Boolean(ownParticipant.isAdmin || ownParticipant.isSuperAdmin) : false,
      };
    });
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

  async sendDocumentMessage(_chatId: string, _media: MediaInput): Promise<MessageResult> {
    throw new Error('sendDocumentMessage not yet implemented in baileys adapter');
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
    this.logger.warn('getContacts not implemented in baileys adapter');
    return [];
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
