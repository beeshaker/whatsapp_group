import {
  toEngineJid,
  toBaileysJid,
  resolveRemoteJid,
  resolveParticipantJid,
  resolveContactJid,
  mapBaileysMessageType,
} from './baileys-jid.util';

describe('baileys-jid.util', () => {
  describe('toEngineJid', () => {
    it('converts an individual @s.whatsapp.net JID to @c.us', () => {
      expect(toEngineJid('254711223344@s.whatsapp.net')).toBe('254711223344@c.us');
    });

    it('strips a device suffix before converting', () => {
      expect(toEngineJid('254711223344:12@s.whatsapp.net')).toBe('254711223344@c.us');
    });

    it('leaves a group JID (@g.us) unchanged', () => {
      expect(toEngineJid('123456789@g.us')).toBe('123456789@g.us');
    });

    it('leaves an opaque @lid JID unchanged (no phone number to recover)', () => {
      expect(toEngineJid('AB12CD34@lid')).toBe('AB12CD34@lid');
    });

    it('returns an empty string for null/undefined input', () => {
      expect(toEngineJid(undefined)).toBe('');
      expect(toEngineJid(null)).toBe('');
    });
  });

  describe('toBaileysJid', () => {
    it('converts an individual @c.us JID to @s.whatsapp.net', () => {
      expect(toBaileysJid('254711223344@c.us')).toBe('254711223344@s.whatsapp.net');
    });

    it('leaves a group JID (@g.us) unchanged', () => {
      expect(toBaileysJid('123456789@g.us')).toBe('123456789@g.us');
    });
  });

  describe('resolveRemoteJid', () => {
    it('prefers remoteJidAlt over remoteJid when both are present', () => {
      expect(resolveRemoteJid({ remoteJid: 'AB12CD34@lid', remoteJidAlt: '254711223344@s.whatsapp.net' })).toBe(
        '254711223344@c.us',
      );
    });

    it('falls back to remoteJid when remoteJidAlt is absent', () => {
      expect(resolveRemoteJid({ remoteJid: '123456789@g.us' })).toBe('123456789@g.us');
    });

    it('returns undefined when neither field is present', () => {
      expect(resolveRemoteJid({})).toBeUndefined();
    });
  });

  describe('resolveParticipantJid', () => {
    it('prefers participantAlt over participant when both are present', () => {
      expect(
        resolveParticipantJid({ participant: 'AB12CD34@lid', participantAlt: '254711223344@s.whatsapp.net' }),
      ).toBe('254711223344@c.us');
    });

    it('returns undefined when neither field is present (e.g. a 1:1 chat)', () => {
      expect(resolveParticipantJid({})).toBeUndefined();
    });
  });

  describe('resolveContactJid', () => {
    it('prefers phoneNumber over id when both are present', () => {
      expect(resolveContactJid({ id: 'AB12CD34@lid', phoneNumber: '254711223344@s.whatsapp.net' })).toBe(
        '254711223344@c.us',
      );
    });

    it('falls back to id when phoneNumber is absent', () => {
      expect(resolveContactJid({ id: '254711223344@s.whatsapp.net' })).toBe('254711223344@c.us');
    });
  });

  describe('mapBaileysMessageType', () => {
    it('maps a plain conversation message to "chat"', () => {
      expect(mapBaileysMessageType({ conversation: 'hi' })).toBe('chat');
    });

    it('maps an extendedTextMessage to "chat"', () => {
      expect(mapBaileysMessageType({ extendedTextMessage: { text: 'hi' } })).toBe('chat');
    });

    it('maps image/video/audio/document content to their backend-recognized type strings', () => {
      expect(mapBaileysMessageType({ imageMessage: {} })).toBe('image');
      expect(mapBaileysMessageType({ videoMessage: {} })).toBe('video');
      expect(mapBaileysMessageType({ audioMessage: {} })).toBe('audio');
      expect(mapBaileysMessageType({ documentMessage: {} })).toBe('document');
    });

    it('returns "unknown" for an unrecognized or missing content type', () => {
      expect(mapBaileysMessageType({ stickerMessage: {} })).toBe('unknown');
      expect(mapBaileysMessageType(null)).toBe('unknown');
      expect(mapBaileysMessageType(undefined)).toBe('unknown');
    });
  });
});
