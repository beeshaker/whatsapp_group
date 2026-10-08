import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { BaileysContactDirectory } from './baileys-contact-directory';

describe('BaileysContactDirectory', () => {
  let tmpDir: string;
  let file: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'contact-dir-test-'));
    file = path.join(tmpDir, 'contacts', 'test.json');
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('maps an @lid (with device suffix) to the phone number learned from a contact', () => {
    const dir = new BaileysContactDirectory(file);
    dir.learnContact({ id: '111@lid', jid: '254711223344:3@s.whatsapp.net', notify: 'Jane' });

    expect(dir.resolve('111:7@lid')).toBe('254711223344@c.us');
    expect(dir.nameFor('111@lid')).toBe('Jane');
    expect(dir.nameFor('254711223344@c.us')).toBe('Jane');
  });

  it('leaves unknown ids alone', () => {
    const dir = new BaileysContactDirectory(file);
    expect(dir.resolve('999@lid')).toBe('999@lid');
    expect(dir.resolve('254700000001@s.whatsapp.net')).toBe('254700000001@c.us');
    expect(dir.resolve(undefined)).toBeUndefined();
    expect(dir.nameFor('999@lid')).toBeUndefined();
  });

  it('prefers the address-book name over the pushName', () => {
    const dir = new BaileysContactDirectory(file);
    dir.learnPushName('254711223344@c.us', 'jd 😎');
    expect(dir.nameFor('254711223344@c.us')).toBe('jd 😎');
    dir.learnContact({ id: '254711223344@s.whatsapp.net', name: 'Jane Rider KMFA' });
    expect(dir.nameFor('254711223344@c.us')).toBe('Jane Rider KMFA');
  });

  it('moves a name learned under an @lid onto the phone number once the mapping arrives', () => {
    const dir = new BaileysContactDirectory(file);
    dir.learnPushName('111@lid', 'Jane');
    dir.learnContact({ lid: '111@lid', jid: '254711223344@s.whatsapp.net' });

    expect(dir.nameFor('254711223344@c.us')).toBe('Jane');
    expect(dir.list()).toEqual([{ id: '254711223344@c.us', pushName: 'Jane' }]);
  });

  it('only lists phone-number contacts', () => {
    const dir = new BaileysContactDirectory(file);
    dir.learnPushName('111@lid', 'Unmapped');
    dir.learnPushName('254711223344@c.us', 'Mapped');
    expect(dir.list().map(e => e.id)).toEqual(['254711223344@c.us']);
  });

  it('persists and reloads', async () => {
    const dir = new BaileysContactDirectory(file);
    dir.learnContact({ id: '111@lid', jid: '254711223344@s.whatsapp.net', name: 'Jane' });
    await dir.save();

    const reloaded = new BaileysContactDirectory(file);
    expect(reloaded.resolve('111@lid')).toBe('254711223344@c.us');
    expect(reloaded.nameFor('111@lid')).toBe('Jane');
  });

  it('does not write when nothing changed', async () => {
    await new BaileysContactDirectory(file).save();
    expect(fs.existsSync(file)).toBe(false);
  });

  it('starts empty on a corrupt file', () => {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, '{not json');
    expect(new BaileysContactDirectory(file).list()).toEqual([]);
  });
});
