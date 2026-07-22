jest.mock('@whiskeysockets/baileys', () => ({
  __esModule: true,
  default: jest.fn(),
  useMultiFileAuthState: jest.fn(),
  downloadMediaMessage: jest.fn(),
}));

jest.mock('../../../engine/adapters/baileys.adapter', () => ({
  BaileysAdapter: jest.fn(),
}));

import { BaileysPlugin } from './index';
import { BaileysAdapter } from '../../../engine/adapters/baileys.adapter';

const mockBaileysAdapter = BaileysAdapter as jest.MockedClass<typeof BaileysAdapter>;

describe('BaileysPlugin', () => {
  afterEach(() => {
    delete process.env.BAILEYS_AUTH_DIR;
    jest.clearAllMocks();
  });

  describe('createEngine', () => {
    it('uses BAILEYS_AUTH_DIR from the environment when no plugin-level config override is set', () => {
      process.env.BAILEYS_AUTH_DIR = '/app/data/baileys';
      const mockInstance = {
        config: { sessionId: 'dunhill', authDir: '/app/data/baileys' },
      };
      mockBaileysAdapter.mockReturnValue(mockInstance as any);

      const plugin = new BaileysPlugin();
      const engine = plugin.createEngine({ sessionId: 'dunhill' });

      expect(mockBaileysAdapter).toHaveBeenCalledWith({
        sessionId: 'dunhill',
        authDir: '/app/data/baileys',
      });
      expect((engine as any).config).toEqual({
        sessionId: 'dunhill',
        authDir: '/app/data/baileys',
      });
    });

    it('falls back to ./data/baileys when BAILEYS_AUTH_DIR is unset', () => {
      const mockInstance = {
        config: { sessionId: 'dunhill', authDir: './data/baileys' },
      };
      mockBaileysAdapter.mockReturnValue(mockInstance as any);

      const plugin = new BaileysPlugin();
      const engine = plugin.createEngine({ sessionId: 'dunhill' });

      expect(mockBaileysAdapter).toHaveBeenCalledWith({
        sessionId: 'dunhill',
        authDir: './data/baileys',
      });
      expect((engine as any).config.authDir).toBe('./data/baileys');
    });
  });

  describe('getFeatures', () => {
    it('reports the Phase-1 feature set', () => {
      const plugin = new BaileysPlugin();
      expect(plugin.getFeatures()).toEqual([
        'text-messages',
        'message-replies',
        'message-reactions',
        'group-management-read',
      ]);
    });
  });
});
