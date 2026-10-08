import * as fs from 'fs';
import * as path from 'path';
import { toEngineJid } from './baileys-jid.util';

/** The subset of a Baileys Contact / GroupParticipant this directory reads. */
export interface DirectoryContactSource {
  id?: string | null;
  lid?: string | null;
  jid?: string | null;
  phoneNumber?: string | null;
  name?: string | null;
  notify?: string | null;
  verifiedName?: string | null;
}

export interface DirectoryEntry {
  /** Phone-number JID in this product's convention ("<number>@c.us"). */
  id: string;
  /** Name saved in the bot phone's address book. */
  savedName?: string;
  /** Name the person set on their own WhatsApp profile (pushName). */
  pushName?: string;
  verifiedName?: string;
}

interface PersistedDirectory {
  lidToPn: Record<string, string>;
  entries: Record<string, DirectoryEntry>;
}

const SAVE_DEBOUNCE_MS = 2000;

/** "<user>[:<device>]@<server>" -> "<user>@<server>" with c.us for phone JIDs. */
function normalize(jid: string | null | undefined): string | undefined {
  if (!jid) return undefined;
  const at = jid.indexOf('@');
  if (at === -1) return undefined;
  const user = jid.slice(0, at).split(':')[0];
  const server = jid.slice(at + 1);
  return toEngineJid(`${user}@${server}`);
}

function isLid(jid: string | undefined): jid is string {
  return Boolean(jid?.endsWith('@lid'));
}

function isPn(jid: string | undefined): jid is string {
  return Boolean(jid?.endsWith('@c.us'));
}

function clean(name: string | null | undefined): string | undefined {
  const trimmed = name?.trim();
  return trimmed ? trimmed : undefined;
}

/**
 * Who-is-who for a Baileys session: maps opaque "@lid" sender ids to phone
 * numbers and phone numbers to display names. Baileys keeps no contact store
 * of its own, and history-sync messages usually carry neither a pushName nor
 * the sender's phone number, so without this every backfilled sender is
 * "Unknown". Fed from contacts/group-metadata events and live messages,
 * persisted next to (not inside) the auth dir so it survives a re-link.
 */
export class BaileysContactDirectory {
  private lidToPn = new Map<string, string>();
  private entries = new Map<string, DirectoryEntry>();
  private saveTimer: NodeJS.Timeout | null = null;
  private dirty = false;

  constructor(private readonly filePath: string) {
    this.load();
  }

  private load(): void {
    try {
      const data = JSON.parse(fs.readFileSync(this.filePath, 'utf8')) as PersistedDirectory;
      this.lidToPn = new Map(Object.entries(data.lidToPn ?? {}));
      this.entries = new Map(Object.entries(data.entries ?? {}));
    } catch {
      // first run, or a torn file -- start empty; it is rebuilt from events
    }
  }

  /** Learns from a Baileys Contact or GroupParticipant. */
  learnContact(contact: DirectoryContactSource): void {
    const ids = [contact.id, contact.lid, contact.jid, contact.phoneNumber].map(normalize);
    const lid = ids.find(isLid);
    const pn = ids.find(isPn);
    let changed = false;
    if (lid && pn && this.lidToPn.get(lid) !== pn) {
      this.lidToPn.set(lid, pn);
      changed = true;
    }
    const key = pn ?? (lid ? this.lidToPn.get(lid) : undefined) ?? lid;
    if (key) {
      changed =
        this.merge(key, {
          savedName: clean(contact.name),
          pushName: clean(contact.notify),
          verifiedName: clean(contact.verifiedName),
        }) || changed;
    }
    if (changed) this.scheduleSave();
  }

  /** Records the pushName seen on a message from `jid`. */
  learnPushName(jid: string | undefined, pushName: string | null | undefined): void {
    const key = this.resolve(jid);
    if (key && this.merge(key, { pushName: clean(pushName) })) this.scheduleSave();
  }

  /** Phone-number JID for an @lid when known, otherwise the input (normalized). */
  resolve(jid: string | null | undefined): string | undefined {
    const normalized = normalize(jid);
    if (isLid(normalized)) return this.lidToPn.get(normalized) ?? normalized;
    return normalized ?? (jid || undefined);
  }

  /** Best display name for a sender: address-book name, then their own, then verified. */
  nameFor(jid: string | null | undefined): string | undefined {
    const key = this.resolve(jid);
    const entry = key ? this.entries.get(key) : undefined;
    return entry ? entry.savedName || entry.pushName || entry.verifiedName : undefined;
  }

  /** Every known phone-number contact (unresolved @lid entries are left out). */
  list(): DirectoryEntry[] {
    return [...this.entries.values()].filter(e => isPn(e.id));
  }

  private merge(key: string, update: Omit<DirectoryEntry, 'id'>): boolean {
    let entry = this.entries.get(key);
    // An entry first learned under its @lid moves to the phone number once known.
    if (!entry && isPn(key)) {
      for (const [lid, pn] of this.lidToPn) {
        const lidEntry = pn === key ? this.entries.get(lid) : undefined;
        if (lidEntry) {
          this.entries.delete(lid);
          entry = { ...lidEntry, id: key };
          this.entries.set(key, entry);
          break;
        }
      }
    }
    if (!entry) {
      entry = { id: key };
      this.entries.set(key, entry);
    }
    let changed = false;
    for (const field of ['savedName', 'pushName', 'verifiedName'] as const) {
      const value = update[field];
      if (value && entry[field] !== value) {
        entry[field] = value;
        changed = true;
      }
    }
    return changed;
  }

  private scheduleSave(): void {
    this.dirty = true;
    if (this.saveTimer) return;
    this.saveTimer = setTimeout(() => {
      this.saveTimer = null;
      void this.save();
    }, SAVE_DEBOUNCE_MS);
    this.saveTimer.unref?.();
  }

  async save(): Promise<void> {
    if (this.saveTimer) {
      clearTimeout(this.saveTimer);
      this.saveTimer = null;
    }
    if (!this.dirty) return;
    this.dirty = false;
    const data: PersistedDirectory = {
      lidToPn: Object.fromEntries(this.lidToPn),
      entries: Object.fromEntries(this.entries),
    };
    await fs.promises.mkdir(path.dirname(this.filePath), { recursive: true });
    const tmp = `${this.filePath}.tmp`;
    await fs.promises.writeFile(tmp, JSON.stringify(data));
    await fs.promises.rename(tmp, this.filePath);
  }
}
