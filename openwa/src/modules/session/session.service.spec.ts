jest.mock('@whiskeysockets/baileys', () => ({
  __esModule: true,
  default: jest.fn(),
  useMultiFileAuthState: jest.fn(),
  downloadMediaMessage: jest.fn(),
}));

import { Test, TestingModule } from '@nestjs/testing';
import { getRepositoryToken, getDataSourceToken } from '@nestjs/typeorm';
import { Repository, DataSource } from 'typeorm';
import { NotFoundException, ConflictException, BadRequestException } from '@nestjs/common';
import { SessionService } from './session.service';
import { Session, SessionStatus } from './entities/session.entity';
import { EngineFactory } from '../../engine/engine.factory';
import { EngineStatus, EngineEventCallbacks } from '../../engine/interfaces/whatsapp-engine.interface';
import { EventsGateway } from '../events/events.gateway';
import { WebhookService } from '../webhook/webhook.service';
import { HookManager } from '../../core/hooks';

async function flushPromises(): Promise<void> {
  for (let i = 0; i < 10; i++) {
    await new Promise(resolve => setImmediate(resolve));
  }
}

function createMockSession(overrides: Partial<Session> = {}): Session {
  return {
    id: 'sess-uuid-1',
    name: 'test-session',
    status: SessionStatus.CREATED,
    phone: null,
    pushName: null,
    config: {},
    proxyUrl: null,
    proxyType: null,
    connectedAt: null,
    lastActiveAt: null,
    createdAt: new Date(),
    updatedAt: new Date(),
    ...overrides,
  };
}

describe('SessionService', () => {
  let service: SessionService;
  let repository: jest.Mocked<Partial<Repository<Session>>>;
  let dataSource: jest.Mocked<Partial<DataSource>>;
  let engineFactory: jest.Mocked<Partial<EngineFactory>>;
  let eventsGateway: jest.Mocked<Partial<EventsGateway>>;
  let webhookService: jest.Mocked<Partial<WebhookService>>;
  let hookManager: jest.Mocked<Partial<HookManager>>;
  let mockEngine: Record<string, jest.Mock>;

  beforeEach(async () => {
    repository = {
      count: jest.fn(),
      find: jest.fn(),
      findOne: jest.fn(),
      create: jest.fn(),
      save: jest.fn(),
      remove: jest.fn(),
      update: jest.fn(),
    };

    dataSource = {
      transaction: jest.fn().mockImplementation(async (cb: (manager: unknown) => Promise<unknown>) => {
        const manager = {
          save: jest.fn().mockImplementation((entity: unknown) => Promise.resolve(entity)),
          remove: jest.fn().mockResolvedValue(undefined),
        };
        return cb(manager);
      }),
    };

    mockEngine = {
      initialize: jest.fn().mockResolvedValue(undefined),
      destroy: jest.fn().mockResolvedValue(undefined),
      disconnect: jest.fn().mockResolvedValue(undefined),
      getQRCode: jest.fn().mockReturnValue(null),
      getGroups: jest.fn().mockResolvedValue([]),
      getStatus: jest.fn().mockReturnValue(EngineStatus.INITIALIZING),
      clearAuthState: jest.fn().mockResolvedValue(undefined),
    };

    engineFactory = {
      create: jest.fn().mockReturnValue(mockEngine),
    };

    eventsGateway = {
      emitSessionStatus: jest.fn(),
      emitMessage: jest.fn(),
    };

    webhookService = {
      dispatch: jest.fn().mockResolvedValue(undefined),
    };

    hookManager = {
      execute: jest.fn().mockResolvedValue({ continue: true, data: {} }),
    };

    const module: TestingModule = await Test.createTestingModule({
      providers: [
        SessionService,
        {
          provide: getRepositoryToken(Session, 'data'),
          useValue: repository,
        },
        {
          provide: getDataSourceToken('data'),
          useValue: dataSource,
        },
        { provide: EngineFactory, useValue: engineFactory },
        { provide: EventsGateway, useValue: eventsGateway },
        { provide: WebhookService, useValue: webhookService },
        { provide: HookManager, useValue: hookManager },
      ],
    }).compile();

    service = module.get<SessionService>(SessionService);
  });

  // ── create ────────────────────────────────────────────────────────

  describe('create', () => {
    it('should create a new session with CREATED status', async () => {
      const session = createMockSession();
      (repository.findOne as jest.Mock).mockResolvedValue(null); // no duplicate
      (repository.create as jest.Mock).mockReturnValue(session);
      (repository.save as jest.Mock).mockResolvedValue(session);

      const result = await service.create({ name: 'test-session' });

      expect(result.name).toBe('test-session');
      expect(repository.create).toHaveBeenCalledWith(expect.objectContaining({ status: SessionStatus.CREATED }));
      expect(hookManager.execute).toHaveBeenCalledWith(
        'session:created',
        session,
        expect.objectContaining({ sessionId: session.id }),
      );
    });

    it('should throw ConflictException if session name already exists', async () => {
      (repository.findOne as jest.Mock).mockResolvedValue(createMockSession());

      await expect(service.create({ name: 'test-session' })).rejects.toThrow(ConflictException);
    });
  });

  // ── findAll / findOne / findByName ────────────────────────────────

  describe('findAll', () => {
    it('should return all sessions ordered by createdAt DESC', async () => {
      const sessions = [createMockSession(), createMockSession({ id: 'sess-2' })];
      (repository.find as jest.Mock).mockResolvedValue(sessions);

      const result = await service.findAll();

      expect(result).toHaveLength(2);
      expect(repository.find).toHaveBeenCalledWith({ order: { createdAt: 'DESC' } });
    });
  });

  describe('findOne', () => {
    it('should return session by id', async () => {
      const session = createMockSession();
      (repository.findOne as jest.Mock).mockResolvedValue(session);

      const result = await service.findOne('sess-uuid-1');
      expect(result.id).toBe('sess-uuid-1');
    });

    it('should throw NotFoundException if session not found', async () => {
      (repository.findOne as jest.Mock).mockResolvedValue(null);

      await expect(service.findOne('nonexistent')).rejects.toThrow(NotFoundException);
    });
  });

  describe('findByName', () => {
    it('should return session by name', async () => {
      const session = createMockSession();
      (repository.findOne as jest.Mock).mockResolvedValue(session);

      const result = await service.findByName('test-session');
      expect(result.name).toBe('test-session');
    });

    it('should throw NotFoundException if name not found', async () => {
      (repository.findOne as jest.Mock).mockResolvedValue(null);

      await expect(service.findByName('nonexistent')).rejects.toThrow(NotFoundException);
    });
  });

  // ── delete ────────────────────────────────────────────────────────

  describe('delete', () => {
    it('should stop engine and remove session from DB', async () => {
      const session = createMockSession();
      (repository.findOne as jest.Mock).mockResolvedValue(session);
      (repository.remove as jest.Mock).mockResolvedValue(session);

      await service.delete('sess-uuid-1');

      expect(hookManager.execute).toHaveBeenCalledWith(
        'session:deleted',
        expect.objectContaining({ id: 'sess-uuid-1', name: 'test-session' }),
        expect.any(Object),
      );
    });

    it('should destroy running engine before deleting', async () => {
      const session = createMockSession();
      (repository.findOne as jest.Mock).mockResolvedValue(session);
      (repository.save as jest.Mock).mockImplementation(s => Promise.resolve(s));
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });
      (repository.remove as jest.Mock).mockResolvedValue(session);

      // Start the session first to create an engine
      await service.start('sess-uuid-1');

      // Now delete
      await service.delete('sess-uuid-1');

      expect(mockEngine.destroy).toHaveBeenCalled();
    });

    it('should wipe saved auth so a re-created session with the same name gets a fresh QR', async () => {
      const session = createMockSession();
      (repository.findOne as jest.Mock).mockResolvedValue(session);

      await service.delete('sess-uuid-1');

      expect(engineFactory.create).toHaveBeenCalledWith({ sessionId: 'test-session' });
      expect(mockEngine.clearAuthState).toHaveBeenCalled();
    });

    it('should still delete when the engine fails to shut down', async () => {
      (repository.findOne as jest.Mock).mockResolvedValue(createMockSession());
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });
      await service.start('sess-uuid-1');
      mockEngine.destroy.mockRejectedValue(new Error('Target closed'));

      await expect(service.delete('sess-uuid-1')).resolves.toBeUndefined();
      expect(service.isActive('sess-uuid-1')).toBe(false);
    });
  });

  // ── start ─────────────────────────────────────────────────────────

  describe('start', () => {
    it('should create engine and set status to INITIALIZING', async () => {
      const session = createMockSession();
      (repository.findOne as jest.Mock).mockResolvedValue(session);
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });

      await service.start('sess-uuid-1');

      expect(engineFactory.create).toHaveBeenCalledWith(expect.objectContaining({ sessionId: 'test-session' }));
      expect(mockEngine.initialize).toHaveBeenCalled();
      expect(repository.update).toHaveBeenCalledWith('sess-uuid-1', {
        status: SessionStatus.INITIALIZING,
      });
    });

    it('should throw BadRequestException if the running engine is still healthy', async () => {
      const session = createMockSession();
      (repository.findOne as jest.Mock).mockResolvedValue(session);
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });

      await service.start('sess-uuid-1');

      await expect(service.start('sess-uuid-1')).rejects.toThrow(BadRequestException);
    });

    it('should replace a FAILED engine instead of refusing with "already started"', async () => {
      (repository.findOne as jest.Mock).mockResolvedValue(createMockSession());
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });
      await service.start('sess-uuid-1');
      mockEngine.getStatus.mockReturnValue(EngineStatus.FAILED);

      await service.start('sess-uuid-1');

      expect(mockEngine.destroy).toHaveBeenCalled();
      expect(engineFactory.create).toHaveBeenCalledTimes(2);
    });

    it('should not block on engine startup and should mark FAILED with lastError if it rejects', async () => {
      (repository.findOne as jest.Mock).mockResolvedValue(createMockSession());
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });
      mockEngine.initialize.mockReturnValue(new Promise(() => undefined)); // never resolves

      await expect(service.start('sess-uuid-1')).resolves.toBeDefined();

      mockEngine.initialize.mockRejectedValue(new Error('Chrome crashed'));
      mockEngine.getStatus.mockReturnValue(EngineStatus.FAILED);
      await service.start('sess-uuid-1');
      await flushPromises();

      expect(repository.update).toHaveBeenCalledWith('sess-uuid-1', { status: SessionStatus.FAILED });
      expect(service.getRuntimeInfo('sess-uuid-1').lastError).toBe('Chrome crashed');
      await service.onModuleDestroy();
    });

    it('should execute session:starting hook before initializing engine', async () => {
      const session = createMockSession();
      (repository.findOne as jest.Mock).mockResolvedValue(session);
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });

      await service.start('sess-uuid-1');

      expect(hookManager.execute).toHaveBeenCalledWith(
        'session:starting',
        expect.objectContaining({ sessionId: 'sess-uuid-1' }),
        expect.any(Object),
      );
    });
  });

  // ── stop ──────────────────────────────────────────────────────────

  describe('stop', () => {
    it('should disconnect engine and set status to DISCONNECTED', async () => {
      const session = createMockSession();
      (repository.findOne as jest.Mock).mockResolvedValue(session);
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });

      // Start first
      await service.start('sess-uuid-1');

      // Stop
      await service.stop('sess-uuid-1');

      expect(mockEngine.disconnect).toHaveBeenCalled();
      expect(repository.update).toHaveBeenCalledWith('sess-uuid-1', {
        status: SessionStatus.DISCONNECTED,
      });
    });
  });

  // ── restart / relink ──────────────────────────────────────────────

  describe('restart', () => {
    it('should tear down even if destroy throws, start a new engine, and keep the saved login', async () => {
      (repository.findOne as jest.Mock).mockResolvedValue(createMockSession());
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });
      await service.start('sess-uuid-1');
      mockEngine.destroy.mockRejectedValue(new Error('Target closed'));

      await service.restart('sess-uuid-1');

      expect(engineFactory.create).toHaveBeenCalledTimes(2);
      expect(mockEngine.clearAuthState).not.toHaveBeenCalled();
      expect(service.isActive('sess-uuid-1')).toBe(true);
    });

    it('should work when no engine is running', async () => {
      (repository.findOne as jest.Mock).mockResolvedValue(createMockSession({ status: SessionStatus.FAILED }));
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });

      await service.restart('sess-uuid-1');

      expect(mockEngine.initialize).toHaveBeenCalled();
    });
  });

  describe('relink', () => {
    it('should wipe saved auth between teardown and the new engine start', async () => {
      (repository.findOne as jest.Mock).mockResolvedValue(createMockSession());
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });
      const order: string[] = [];
      mockEngine.destroy.mockImplementation(() => order.push('destroy'));
      mockEngine.clearAuthState.mockImplementation(() => order.push('clear'));
      mockEngine.initialize.mockImplementation(() => {
        order.push('init');
        return Promise.resolve();
      });
      await service.start('sess-uuid-1');
      order.length = 0;

      await service.relink('sess-uuid-1');

      expect(order).toEqual(['destroy', 'clear', 'init']);
    });
  });

  // ── disconnect handling ───────────────────────────────────────────

  describe('disconnect handling', () => {
    function makeEngine(): Record<string, jest.Mock> {
      return {
        initialize: jest.fn().mockResolvedValue(undefined),
        destroy: jest.fn().mockResolvedValue(undefined),
        disconnect: jest.fn().mockResolvedValue(undefined),
        getQRCode: jest.fn().mockReturnValue(null),
        getStatus: jest.fn().mockReturnValue(EngineStatus.READY),
        clearAuthState: jest.fn().mockResolvedValue(undefined),
      };
    }

    let created: Record<string, jest.Mock>[];
    const callbacksOf = (engine: Record<string, jest.Mock>): EngineEventCallbacks =>
      (engine.initialize.mock.calls as EngineEventCallbacks[][])[0][0];

    beforeEach(() => {
      jest.useFakeTimers({ doNotFake: ['setImmediate', 'nextTick'] });
      created = [];
      (engineFactory.create as jest.Mock).mockImplementation(() => {
        const engine = makeEngine();
        created.push(engine);
        return engine;
      });
      (repository.findOne as jest.Mock).mockResolvedValue(createMockSession());
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });
    });

    afterEach(async () => {
      await service.onModuleDestroy();
      jest.useRealTimers();
    });

    it('on a logged-out disconnect: wipes auth and restarts straight into a fresh QR, no backoff', async () => {
      await service.start('sess-uuid-1');
      callbacksOf(created[0]).onDisconnected!('LOGOUT', { loggedOut: true });
      await flushPromises();

      // created[1] is the throwaway engine used to clear auth, created[2] the new live one
      expect(created[0].destroy).toHaveBeenCalled();
      expect(created[1].clearAuthState).toHaveBeenCalled();
      expect(created[2].initialize).toHaveBeenCalled();
      expect(service.getRuntimeInfo('sess-uuid-1')).toMatchObject({
        needsRelink: true,
        lastDisconnectReason: 'LOGOUT',
      });
      expect(jest.getTimerCount()).toBe(0);
    });

    it('clears needsRelink once the new QR is scanned', async () => {
      await service.start('sess-uuid-1');
      callbacksOf(created[0]).onDisconnected!('LOGOUT', { loggedOut: true });
      await flushPromises();

      callbacksOf(created[2]).onReady!('254700000000', 'Bot');

      expect(service.getRuntimeInfo('sess-uuid-1').needsRelink).toBe(false);
    });

    it('on a transient disconnect: keeps auth and reconnects after backoff', async () => {
      await service.start('sess-uuid-1');
      callbacksOf(created[0]).onDisconnected!('NAVIGATION', { loggedOut: false });

      expect(created.length).toBe(1);
      await jest.advanceTimersByTimeAsync(7000);

      expect(created.length).toBe(2);
      expect(created[0].clearAuthState).not.toHaveBeenCalled();
      expect(created[1].initialize).toHaveBeenCalled();
    });

    it('reconnects almost immediately when the engine reports restartRequired', async () => {
      await service.start('sess-uuid-1');
      callbacksOf(created[0]).onDisconnected!('restart required (515)', { restartRequired: true });

      await jest.advanceTimersByTimeAsync(1100);

      expect(created.length).toBe(2);
    });

    it('marks FAILED after max attempts but keeps retrying slowly instead of giving up', async () => {
      (repository.findOne as jest.Mock).mockResolvedValue(
        createMockSession({ config: { maxReconnectAttempts: 1, reconnectBaseDelay: 10 } }),
      );
      await service.start('sess-uuid-1');

      callbacksOf(created[0]).onDisconnected!('drop');
      await jest.advanceTimersByTimeAsync(1100); // attempt 1
      callbacksOf(created[1]).onDisconnected!('drop');
      await flushPromises();

      expect(repository.update).toHaveBeenCalledWith('sess-uuid-1', { status: SessionStatus.FAILED });
      await jest.advanceTimersByTimeAsync(5 * 60 * 1000);
      expect(created.length).toBe(3);
    });

    it('ignores late events from a torn-down engine (stale-callback guard)', async () => {
      await service.start('sess-uuid-1');
      const oldCallbacks = callbacksOf(created[0]);
      await service.restart('sess-uuid-1');
      (repository.update as jest.Mock).mockClear();

      oldCallbacks.onStateChanged!(EngineStatus.DISCONNECTED);
      oldCallbacks.onDisconnected!('NAVIGATION');

      expect(repository.update).not.toHaveBeenCalled();
      expect(jest.getTimerCount()).toBe(0);
    });
  });

  // ── getQRCode ─────────────────────────────────────────────────────

  describe('getQRCode', () => {
    it('should return status with a null QR (not throw) when the engine is not started', async () => {
      const session = createMockSession({ status: SessionStatus.FAILED });
      (repository.findOne as jest.Mock).mockResolvedValue(session);

      await expect(service.getQRCode('sess-uuid-1')).resolves.toEqual({
        qrCode: null,
        status: SessionStatus.FAILED,
        lastError: null,
        needsRelink: false,
      });
    });

    it('should return QR code from engine', async () => {
      const session = createMockSession({ status: SessionStatus.QR_READY });
      (repository.findOne as jest.Mock).mockResolvedValue(session);
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });

      await service.start('sess-uuid-1');
      mockEngine.getQRCode.mockReturnValue('data:image/png;base64,iVBOR...');

      const result = await service.getQRCode('sess-uuid-1');

      expect(result.qrCode).toBe('data:image/png;base64,iVBOR...');
    });

    it('should report READY with a null QR once authenticated', async () => {
      const session = createMockSession({ status: SessionStatus.READY });
      (repository.findOne as jest.Mock).mockResolvedValue(session);
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });

      await service.start('sess-uuid-1');
      mockEngine.getQRCode.mockReturnValue(null);

      await expect(service.getQRCode('sess-uuid-1')).resolves.toMatchObject({
        qrCode: null,
        status: SessionStatus.READY,
      });
    });
  });

  // ── getStats ──────────────────────────────────────────────────────

  describe('getStats', () => {
    it('should return correct session statistics', async () => {
      const sessions = [
        createMockSession({ status: SessionStatus.READY }),
        createMockSession({ id: 'sess-2', status: SessionStatus.READY }),
        createMockSession({ id: 'sess-3', status: SessionStatus.DISCONNECTED }),
      ];
      (repository.find as jest.Mock).mockResolvedValue(sessions);

      const stats = await service.getStats();

      expect(stats.total).toBe(3);
      expect(stats.ready).toBe(2);
      expect(stats.disconnected).toBe(1);
      expect(stats.byStatus[SessionStatus.READY]).toBe(2);
      expect(stats.memoryUsage).toBeDefined();
    });
  });

  // ── getActiveCount / isActive ─────────────────────────────────────

  describe('getActiveCount', () => {
    it('should return 0 when no engines are running', () => {
      expect(service.getActiveCount()).toBe(0);
    });

    it('should return correct count after starting sessions', async () => {
      const session = createMockSession();
      (repository.findOne as jest.Mock).mockResolvedValue(session);
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });

      await service.start('sess-uuid-1');

      expect(service.getActiveCount()).toBe(1);
    });
  });

  describe('isActive', () => {
    it('should return false for inactive session', () => {
      expect(service.isActive('nonexistent')).toBe(false);
    });

    it('should return true for active session', async () => {
      const session = createMockSession();
      (repository.findOne as jest.Mock).mockResolvedValue(session);
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });

      await service.start('sess-uuid-1');

      expect(service.isActive('sess-uuid-1')).toBe(true);
    });
  });

  // ── onModuleInit ──────────────────────────────────────────────────

  describe('onModuleInit', () => {
    it('should reset active sessions to DISCONNECTED on startup', async () => {
      (repository.update as jest.Mock).mockResolvedValue({ affected: 3 });

      await service.onModuleInit();

      expect(repository.update).toHaveBeenCalledWith(expect.objectContaining({ status: expect.anything() as string }), {
        status: SessionStatus.DISCONNECTED,
      });
    });
  });

  describe('getHistory', () => {
    it('delegates to the engine, even when the session is not running', async () => {
      (repository.findOne as jest.Mock).mockResolvedValue(createMockSession());
      const history = [{ id: 'h-1', timestamp: 1 }];
      mockEngine.getHistory = jest.fn().mockResolvedValue(history);

      await expect(service.getHistory('sess-uuid-1', { since: 1 })).resolves.toBe(history);
      expect(engineFactory.create).toHaveBeenCalledWith({ sessionId: 'test-session' });
      expect(mockEngine.getHistory).toHaveBeenCalledWith({ since: 1 });
    });

    it('rejects with 400 for an engine without a history source', async () => {
      (repository.findOne as jest.Mock).mockResolvedValue(createMockSession());

      await expect(service.getHistory('sess-uuid-1', {})).rejects.toThrow(BadRequestException);
    });
  });

  describe('onApplicationBootstrap', () => {
    it('auto-starts every previously linked session, including FAILED ones, and skips unlinked', async () => {
      const linked = createMockSession({ id: 'a', phone: '254700000000', status: SessionStatus.FAILED });
      const unlinked = createMockSession({ id: 'b', name: 'other', phone: null });
      (repository.find as jest.Mock).mockResolvedValue([linked, unlinked]);
      (repository.findOne as jest.Mock).mockImplementation(({ where: { id } }: { where: { id: string } }) =>
        Promise.resolve(id === 'a' ? linked : unlinked),
      );
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });

      await service.onApplicationBootstrap();

      expect(service.isActive('a')).toBe(true);
      expect(service.isActive('b')).toBe(false);
    });
  });

  // ── onModuleDestroy ───────────────────────────────────────────────

  describe('onModuleDestroy', () => {
    it('should destroy all running engines on shutdown', async () => {
      const session = createMockSession();
      (repository.findOne as jest.Mock).mockResolvedValue(session);
      (repository.update as jest.Mock).mockResolvedValue({ affected: 1 });

      await service.start('sess-uuid-1');
      await service.onModuleDestroy();

      expect(mockEngine.destroy).toHaveBeenCalled();
      expect(service.getActiveCount()).toBe(0);
    });
  });
});
