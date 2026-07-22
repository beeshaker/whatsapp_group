const ENGINE_INDIVIDUAL_DOMAIN = 'c.us';
const BAILEYS_INDIVIDUAL_DOMAIN = 's.whatsapp.net';

/**
 * Converts a Baileys-native JID (individual: "<number>[:<device>]@s.whatsapp.net",
 * group: "<id>@g.us") to this product's existing convention (individual:
 * "<number>@c.us", group unchanged). Anything that isn't an individual
 * @s.whatsapp.net address (groups, @lid, broadcast, etc.) passes through unchanged.
 */
export function toEngineJid(baileysJid: string | undefined | null): string {
  if (!baileysJid) {
    return '';
  }
  const atIndex = baileysJid.indexOf('@');
  if (atIndex === -1) {
    return baileysJid;
  }
  const domain = baileysJid.slice(atIndex + 1);
  if (domain !== BAILEYS_INDIVIDUAL_DOMAIN) {
    return baileysJid;
  }
  const bareNumber = baileysJid.slice(0, atIndex).split(':')[0];
  return `${bareNumber}@${ENGINE_INDIVIDUAL_DOMAIN}`;
}

/**
 * Converts this product's "<number>@c.us" convention to the Baileys-native
 * "<number>@s.whatsapp.net" individual JID. Group JIDs ("@g.us") pass through
 * unchanged since both engines use the same group JID format.
 */
export function toBaileysJid(engineJid: string): string {
  const atIndex = engineJid.indexOf('@');
  if (atIndex === -1) {
    return engineJid;
  }
  const domain = engineJid.slice(atIndex + 1);
  if (domain !== ENGINE_INDIVIDUAL_DOMAIN) {
    return engineJid;
  }
  return `${engineJid.slice(0, atIndex)}@${BAILEYS_INDIVIDUAL_DOMAIN}`;
}

interface RemoteJidSource {
  remoteJid?: string | null;
  remoteJidAlt?: string | null;
}

interface ParticipantJidSource {
  participant?: string | null;
  participantAlt?: string | null;
}

/**
 * Baileys 7.x's WAMessageKey carries both a primary field (which may be an
 * opaque "@lid" linked-device identifier) and an "Alt" field (the real
 * phone-number JID), when WhatsApp has disclosed the phone-number mapping.
 * Always prefer the phone-number form so replyToMessage's authorHint
 * (a bare phone number from the backend) can actually match.
 */
export function resolveRemoteJid(key: RemoteJidSource): string | undefined {
  const raw = key.remoteJidAlt || key.remoteJid;
  return raw ? toEngineJid(raw) : undefined;
}

export function resolveParticipantJid(key: ParticipantJidSource): string | undefined {
  const raw = key.participantAlt || key.participant;
  return raw ? toEngineJid(raw) : undefined;
}

interface ContactJidSource {
  id: string;
  phoneNumber?: string;
}

/**
 * Same @lid-vs-phone-number preference as resolveParticipantJid, applied to
 * group participant Contact entries (GroupMetadata.participants).
 */
export function resolveContactJid(contact: ContactJidSource): string {
  return toEngineJid(contact.phoneNumber || contact.id);
}

const BAILEYS_MEDIA_TYPE_MAP: Record<string, string> = {
  imageMessage: 'image',
  videoMessage: 'video',
  documentMessage: 'document',
  audioMessage: 'audio',
};

/**
 * Maps a Baileys message content object to the type string this product's
 * backend already branches on (backend/main.py: msg_type == "chat" for text,
 * msg_type in {"image","video","document","audio"} for media — this is
 * whatsapp-web.js's own type vocabulary, which the backend hardcodes) —
 * Baileys' different content-key names must be translated here rather than
 * passed through raw.
 */
export function mapBaileysMessageType(messageContent: Record<string, unknown> | null | undefined): string {
  if (!messageContent) {
    return 'unknown';
  }
  if ('conversation' in messageContent || 'extendedTextMessage' in messageContent) {
    return 'chat';
  }
  const contentKey = Object.keys(messageContent).find(key => key in BAILEYS_MEDIA_TYPE_MAP);
  return contentKey ? BAILEYS_MEDIA_TYPE_MAP[contentKey] : 'unknown';
}
