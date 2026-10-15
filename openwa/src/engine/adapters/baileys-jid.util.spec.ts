import {
  toEngineJid,
  toBaileysJid,
  resolveRemoteJid,
  resolveParticipantJid,
  resolveContactJid,
  mapBaileysMessageType,
  unwrapMessageContent,
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

    it('uses Baileys 6.x senderPn for an @lid 1:1 chat', () => {
      expect(resolveRemoteJid({ remoteJid: 'AB12CD34@lid', senderPn: '254711223344@s.whatsapp.net' })).toBe(
        '254711223344@c.us',
      );
    });

    it('ignores senderPn for a group chat', () => {
      expect(resolveRemoteJid({ remoteJid: '123456789@g.us', senderPn: '254711223344@s.whatsapp.net' })).toBe(
        '123456789@g.us',
      );
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

    it('uses Baileys 6.x participantPn when participant is an @lid', () => {
      expect(resolveParticipantJid({ participant: 'AB12CD34@lid', participantPn: '254711223344@s.whatsapp.net' })).toBe(
        '254711223344@c.us',
      );
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

    it('uses Baileys 6.x jid when phoneNumber is absent', () => {
      expect(resolveContactJid({ id: 'AB12CD34@lid', jid: '254711223344@s.whatsapp.net' })).toBe('254711223344@c.us');
    });

    it('falls back to id when phoneNumber is absent', () => {
      expect(resolveContactJid({ id: '254711223344@s.whatsapp.net' })).toBe('254711223344@c.us');
    });
  });

  describe('unwrapMessageContent', () => {
    const image = { imageMessage: { caption: 'KMGQ 372A' } };

    it('returns plain content unchanged', () => {
      expect(unwrapMessageContent(image)).toBe(image);
    });

    it('unwraps a disappearing-message (ephemeral) wrapper, text and media alike', () => {
      expect(unwrapMessageContent({ ephemeralMessage: { message: image } })).toBe(image);
      const text = { conversation: 'brakes gone' };
      expect(mapBaileysMessageType(unwrapMessageContent({ ephemeralMessage: { message: text } }))).toBe('chat');
    });

    it('unwraps a document sent with a caption', () => {
      const doc = { documentMessage: { mimetype: 'image/jpeg', caption: 'hd photo' } };
      expect(unwrapMessageContent({ documentWithCaptionMessage: { message: doc } })).toBe(doc);
    });

    it('drops view-once media, wrapped or flagged, even inside a disappearing message', () => {
      expect(unwrapMessageContent({ viewOnceMessageV2: { message: image } })).toBeUndefined();
      expect(
        unwrapMessageContent({ ephemeralMessage: { message: { viewOnceMessage: { message: image } } } }),
      ).toBeUndefined();
      expect(unwrapMessageContent({ imageMessage: { viewOnce: true } })).toBeUndefined();
      expect(mapBaileysMessageType(unwrapMessageContent({ viewOnceMessageV2: { message: image } }))).toBe('unknown');
    });

    it('handles missing content', () => {
      expect(unwrapMessageContent(undefined)).toBeUndefined();
      expect(unwrapMessageContent(null)).toBeUndefined();
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
