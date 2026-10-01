import {
  Injectable,
  NotFoundException,
  ConflictException,
  BadRequestException,
  OnModuleDestroy,
  OnModuleInit,
  OnApplicationBootstrap,
} from '@nestjs/common';
import { InjectRepository, InjectDataSource } from '@nestjs/typeorm';
import { Repository, In, DataSource } from 'typeorm';
import { Session, SessionStatus } from './entities/session.entity';
import { CreateSessionDto } from './dto';
import { EngineFactory } from '../../engine/engine.factory';
import { IWhatsAppEngine, EngineStatus, DisconnectMeta } from '../../engine/interfaces/whatsapp-engine.interface';
import { createLogger } from '../../common/services/logger.service';
import { EventsGateway } from '../events/events.gateway';
import { WebhookService } from '../webhook/webhook.service';
import { HookManager } from '../../core/hooks';

interface ReconnectState {
  attempts: number;
  timer: NodeJS.Timeout | null;
  maxAttempts: number;
  baseDelay: number;
}

/**
 * Diagnostics kept in memory (not persisted) so the tenant/admin UIs can
 * explain why a session isn't connected. Repopulated naturally after a
 * restart because linked sessions are auto-started on boot.
 */
export interface SessionRuntimeInfo {
  lastError: string | null;
  lastDisconnectReason: string | null;
  // WhatsApp revoked this device; a fresh QR is (being) shown and someone
  // must scan it -- no amount of retrying will reconnect on its own.
  needsRelink: boolean;
}

export interface QRCodeState {
  qrCode: string | null;
  status: SessionStatus;
  lastError: string | null;
  needsRelink: boolean;
}

// After the fast exponential-backoff attempts are used up, keep trying at
// this interval instead of giving up for good (a dropped session would
// otherwise stay offline until someone noticed and clicked Reconnect).
const SLOW_RETRY_DELAY_MS = 5 * 60 * 1000;

@Injectable()
export class SessionService implements OnModuleDestroy, OnModuleInit, OnApplicationBootstrap {
  private readonly logger = createLogger('SessionService');

  // In-memory map of active engine instances
  private engines: Map<string, IWhatsAppEngine> = new Map();

  // Reconnection state per session
  private reconnectStates: Map<string, ReconnectState> = new Map();

  private runtime: Map<string, SessionRuntimeInfo> = new Map();

  constructor(
    @InjectRepository(Session, 'data')
    private readonly sessionRepository: Repository<Session>,
    @InjectDataSource('data')
    private readonly dataSource: DataSource,
    private readonly engineFactory: EngineFactory,
    private readonly eventsGateway: EventsGateway,
    private readonly webhookService: WebhookService,
    private readonly hookManager: HookManager,
  ) {}

  /**
   * On backend startup, reset all active session statuses to disconnected
   * because the engines are not running yet after restart
   */
  async onModuleInit(): Promise<void> {
    const activeStatuses = [
      SessionStatus.READY,
      SessionStatus.INITIALIZING,
      SessionStatus.QR_READY,
      SessionStatus.AUTHENTICATING,
    ];

    const result = await this.sessionRepository.update(
      { status: In(activeStatuses) },
      { status: SessionStatus.DISCONNECTED },
    );

    if (result.affected && result.affected > 0) {
      this.logger.log(`Reset ${result.affected} session(s) to disconnected on startup`, {
        action: 'startup_reset',
        affected: result.affected,
      });
    }
  }

  /**
   * Bring previously-linked sessions back up after a container/Docker restart
   * (including ones left FAILED), so a restart never leaves a tenant offline
   * until someone clicks Reconnect. Runs after every module's onModuleInit so
   * the engine plugins are registered.
   */
  async onApplicationBootstrap(): Promise<void> {
    const sessions = await this.sessionRepository.find();
    for (const session of sessions) {
      if (!session.phone) continue;
      this.logger.log(`Auto-starting previously linked session: ${session.name}`, {
        sessionId: session.id,
        action: 'auto_start',
      });
      await this.start(session.id).catch((error: unknown) => {
        this.recordError(session.id, error);
      });
    }
  }

  async onModuleDestroy(): Promise<void> {
    // Clear all reconnect timers
    for (const [, state] of this.reconnectStates) {
      if (state.timer) {
        clearTimeout(state.timer);
      }
    }
    this.reconnectStates.clear();

    // Clean up all engines on shutdown
    for (const sessionId of [...this.engines.keys()]) {
      this.logger.log(`Destroying engine for session ${sessionId}`, {
        sessionId,
        action: 'shutdown',
      });
      await this.teardownEngine(sessionId);
    }
  }

  async create(dto: CreateSessionDto): Promise<Session> {
    // Check if session with same name exists
    const existing = await this.sessionRepository.findOne({
      where: { name: dto.name },
    });

    if (existing) {
      throw new ConflictException(`Session with name '${dto.name}' already exists`);
    }

    const session = this.sessionRepository.create({
      name: dto.name,
      config: dto.config || {},
      proxyUrl: dto.proxyUrl || null,
      proxyType: dto.proxyType || null,
      status: SessionStatus.CREATED,
    });

    const saved = await this.dataSource.transaction(async manager => {
      return await manager.save(session);
    });
    this.logger.log(`Session created: ${saved.name}`, {
      sessionId: saved.id,
      action: 'create',
    });

    // Execute hook after session created (outside transaction since hooks do external I/O)
    await this.hookManager.execute('session:created', saved, {
      sessionId: saved.id,
      source: 'SessionService',
    });

    return saved;
  }

  async findAll(): Promise<Session[]> {
    return this.sessionRepository.find({
      order: { createdAt: 'DESC' },
    });
  }

  async findOne(id: string): Promise<Session> {
    const session = await this.sessionRepository.findOne({ where: { id } });
    if (!session) {
      throw new NotFoundException(`Session with id '${id}' not found`);
    }
    return session;
  }

  async findByName(name: string): Promise<Session> {
    const session = await this.sessionRepository.findOne({ where: { name } });
    if (!session) {
      throw new NotFoundException(`Session with name '${name}' not found`);
    }
    return session;
  }

  async delete(id: string): Promise<void> {
    const session = await this.findOne(id);

    // Cancel any reconnection attempts
    this.cancelReconnect(id);

    // Stop engine if running
    await this.teardownEngine(id);

    // Saved auth is keyed by session *name*, so it outlives the DB row. Wipe it,
    // otherwise re-creating a session with the same name silently reloads the
    // old (possibly dead) login instead of showing a fresh QR.
    await this.clearAuthState(session);
    this.runtime.delete(id);

    // Execute hook BEFORE delete so plugins can access session data
    await this.hookManager.execute(
      'session:deleted',
      {
        id: session.id,
        name: session.name,
        phone: session.phone,
        pushName: session.pushName,
      },
      {
        sessionId: id,
        source: 'SessionService',
      },
    );

    await this.dataSource.transaction(async manager => {
      await manager.remove(session);
    });
    this.logger.log(`Session deleted: ${session.name}`, {
      sessionId: id,
      action: 'delete',
    });
  }

  async start(id: string): Promise<Session> {
    const session = await this.findOne(id);

    const existing = this.engines.get(id);
    if (existing) {
      const engineStatus = existing.getStatus();
      if (engineStatus !== EngineStatus.FAILED && engineStatus !== EngineStatus.DISCONNECTED) {
        throw new BadRequestException('Session is already started');
      }
      // A dead engine is still registered -- replace it rather than refusing.
      this.cancelReconnect(id);
      await this.teardownEngine(id);
    }

    await this.boot(id, session);
    return this.findOne(id);
  }

  /**
   * Soft reconnect: tear the engine down and start it again, keeping the saved
   * login. Fixes transient drops without a QR scan. Safe in any state.
   */
  async restart(id: string): Promise<Session> {
    const session = await this.findOne(id);
    this.logger.log(`Restarting session: ${session.name}`, { sessionId: id, action: 'restart' });
    this.cancelReconnect(id);
    await this.teardownEngine(id);
    await this.boot(id, session);
    return this.findOne(id);
  }

  /**
   * Hard reconnect: tear down, wipe the saved login, and start fresh so a new
   * QR is shown. Use when a restart doesn't recover, or to link another phone.
   */
  async relink(id: string): Promise<Session> {
    const session = await this.findOne(id);
    this.logger.log(`Re-linking session (auth wiped): ${session.name}`, { sessionId: id, action: 'relink' });
    this.cancelReconnect(id);
    await this.teardownEngine(id);
    await this.clearAuthState(session);
    await this.boot(id, session);
    return this.findOne(id);
  }

  private async boot(id: string, session: Session): Promise<void> {
    // Execute hook before starting
    await this.hookManager.execute(
      'session:starting',
      { sessionId: id },
      {
        sessionId: id,
        source: 'SessionService',
      },
    );

    // Initialize reconnect state
    const config = session.config as {
      maxReconnectAttempts?: number;
      reconnectBaseDelay?: number;
    } | null;
    this.reconnectStates.set(id, {
      attempts: 0,
      timer: null,
      maxAttempts: config?.maxReconnectAttempts ?? 5,
      baseDelay: config?.reconnectBaseDelay ?? 5000,
    });
    const info = this.getRuntimeInfo(id);
    info.lastError = null;

    await this.initializeEngine(id, session);
  }

  /**
   * Creates and registers the engine, then starts it in the background.
   * Engine startup (especially headless Chrome) can take far longer than an
   * HTTP client will wait, so callers get INITIALIZING back immediately and
   * follow progress via status / QR polling.
   */
  private async initializeEngine(id: string, session: Session): Promise<void> {
    this.logger.log(`Initializing engine for session: ${session.name}`, {
      sessionId: id,
      action: 'engine_init',
      proxyEnabled: !!session.proxyUrl,
    });

    const engine = this.engineFactory.create({
      sessionId: session.name,
      proxyUrl: session.proxyUrl || undefined,
      proxyType: session.proxyType || undefined,
    });
    this.engines.set(id, engine);
    await this.updateStatus(id, SessionStatus.INITIALIZING);

    // Every callback first checks it still belongs to the current engine, so a
    // torn-down engine's late events can't overwrite the new engine's status
    // or schedule duplicate reconnects.
    const isCurrent = (): boolean => this.engines.get(id) === engine;

    engine
      .initialize({
        onQRCode: (): void => {
          if (!isCurrent()) return;
          this.logger.log('QR code generated', {
            sessionId: id,
            action: 'qr_generated',
          });

          // Execute hook for QR event
          void this.hookManager.execute(
            'session:qr',
            { sessionId: id },
            {
              sessionId: id,
              source: 'Engine',
            },
          );

          void this.updateStatus(id, SessionStatus.QR_READY);
        },
        onReady: (phone: string, pushName: string): void => {
          if (!isCurrent()) return;
          this.logger.log(`Session ready: ${phone}`, {
            sessionId: id,
            phone,
            pushName,
            action: 'ready',
          });

          // Execute hook for ready event
          void this.hookManager.execute(
            'session:ready',
            { phone, pushName },
            {
              sessionId: id,
              source: 'Engine',
            },
          );

          // Reset reconnect attempts on successful connection
          const reconnectState = this.reconnectStates.get(id);
          if (reconnectState) {
            reconnectState.attempts = 0;
          }
          const info = this.getRuntimeInfo(id);
          info.lastError = null;
          info.needsRelink = false;

          void this.sessionRepository.update(id, {
            status: SessionStatus.READY,
            phone,
            pushName,
            connectedAt: new Date(),
            lastActiveAt: new Date(),
          });
          this.eventsGateway.emitSessionStatus(id, SessionStatus.READY);
        },
        onMessage: (message): void => {
          if (!isCurrent()) return;
          this.logger.debug(`Message received from ${message.from}`, {
            sessionId: id,
            messageId: message.id,
            from: message.from,
            action: 'message_received',
          });
          // Update last active timestamp
          void this.sessionRepository.update(id, { lastActiveAt: new Date() });
          // Convert IncomingMessage to plain object for dispatch
          const messageData = { ...message };

          // Execute hook for message received - plugins can modify or stop processing
          void this.hookManager
            .execute('message:received', messageData, {
              sessionId: id,
              source: 'Engine',
            })
            .then(({ continue: shouldContinue, data: finalMessage }) => {
              if (!shouldContinue) {
                // Plugin stopped processing (e.g., auto-reply handled it)
                return;
              }

              // Dispatch to webhooks with potentially modified message
              void this.webhookService.dispatch(id, 'message.received', finalMessage);
              // Emit real-time event to WebSocket clients
              this.eventsGateway.emitMessage(id, finalMessage);
            });
        },
        onMessageReaction: (reaction): void => {
          if (!isCurrent()) return;
          this.logger.debug(`Reaction received: ${reaction.emoji}`, {
            sessionId: id,
            action: 'reaction_received',
          });
          void this.hookManager
            .execute('message:reaction', { ...reaction }, { sessionId: id, source: 'Engine' })
            .then(({ continue: shouldContinue, data: finalReaction }) => {
              if (!shouldContinue) {
                return;
              }
              void this.webhookService.dispatch(id, 'message.reaction', finalReaction);
            });
        },
        onDisconnected: (reason: string, meta?: DisconnectMeta): void => {
          if (!isCurrent()) return;
          this.handleDisconnected(id, reason, meta);
        },
        onStateChanged: (engineState: EngineStatus): void => {
          if (!isCurrent()) return;
          const statusMap: Record<EngineStatus, SessionStatus> = {
            [EngineStatus.DISCONNECTED]: SessionStatus.DISCONNECTED,
            [EngineStatus.INITIALIZING]: SessionStatus.INITIALIZING,
            [EngineStatus.QR_READY]: SessionStatus.QR_READY,
            [EngineStatus.AUTHENTICATING]: SessionStatus.AUTHENTICATING,
            [EngineStatus.READY]: SessionStatus.READY,
            [EngineStatus.FAILED]: SessionStatus.FAILED,
          };
          const newStatus = statusMap[engineState];
          if (newStatus) {
            void this.updateStatus(id, newStatus);
          }
        },
      })
      .catch((error: unknown) => {
        if (!isCurrent()) return;
        this.recordError(id, error);
        void this.updateStatus(id, SessionStatus.FAILED);
        this.scheduleReconnect(id);
      });
  }

  private handleDisconnected(id: string, reason: string, meta?: DisconnectMeta): void {
    this.logger.warn(`Session disconnected: ${reason}`, {
      sessionId: id,
      reason,
      loggedOut: !!meta?.loggedOut,
      action: 'disconnected',
    });
    this.getRuntimeInfo(id).lastDisconnectReason = reason;

    // Execute hook for disconnected event
    void this.hookManager.execute(
      'session:disconnected',
      { reason },
      {
        sessionId: id,
        source: 'Engine',
      },
    );

    if (meta?.loggedOut) {
      // The saved login is dead. Retrying with it only loops; go straight to a
      // fresh QR so whoever opens the reconnect page can just scan.
      void this.recoverFromLogout(id);
      return;
    }

    void this.updateStatus(id, SessionStatus.DISCONNECTED);
    this.scheduleReconnect(id, { immediate: meta?.restartRequired });
  }

  private async recoverFromLogout(id: string): Promise<void> {
    this.getRuntimeInfo(id).needsRelink = true;
    try {
      const session = await this.findOne(id);
      const state = this.reconnectStates.get(id);
      if (state?.timer) {
        clearTimeout(state.timer);
        state.timer = null;
      }
      await this.teardownEngine(id);
      await this.clearAuthState(session);
      await this.initializeEngine(id, session);
    } catch (error: unknown) {
      this.recordError(id, error);
      void this.updateStatus(id, SessionStatus.FAILED);
    }
  }

  private scheduleReconnect(id: string, options: { immediate?: boolean } = {}): void {
    const state = this.reconnectStates.get(id);
    if (!state) return;
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }

    let delay: number;
    if (options.immediate) {
      delay = 1000;
    } else if (state.attempts >= state.maxAttempts) {
      if (state.attempts === state.maxAttempts) {
        this.logger.error(
          `Max reconnect attempts reached; retrying every ${SLOW_RETRY_DELAY_MS / 60000} min`,
          undefined,
          {
            sessionId: id,
            attempts: state.attempts,
            action: 'reconnect_failed',
          },
        );
        void this.updateStatus(id, SessionStatus.FAILED);
      }
      state.attempts++;
      delay = SLOW_RETRY_DELAY_MS;
    } else {
      // Exponential backoff: baseDelay * 2^attempts (with jitter)
      delay = state.baseDelay * Math.pow(2, state.attempts) + Math.random() * 1000;
      state.attempts++;
    }

    this.logger.log(`Scheduling reconnect attempt ${state.attempts} in ${Math.round(delay / 1000)}s`, {
      sessionId: id,
      attempt: state.attempts,
      delayMs: delay,
      action: 'reconnect_scheduled',
    });

    state.timer = setTimeout(() => {
      state.timer = null;
      void this.executeReconnect(id);
    }, delay);
  }

  private async executeReconnect(id: string): Promise<void> {
    try {
      const session = await this.findOne(id);
      await this.teardownEngine(id);
      await this.initializeEngine(id, session);
    } catch (error: unknown) {
      this.recordError(id, error);
      // Schedule another attempt
      this.scheduleReconnect(id);
    }
  }

  private cancelReconnect(id: string): void {
    const state = this.reconnectStates.get(id);
    if (state?.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    this.reconnectStates.delete(id);
  }

  /**
   * Unregisters the engine *before* destroying it (so its teardown events are
   * ignored by the isCurrent() guard) and never throws -- a half-dead engine
   * must not block the next start.
   */
  private async teardownEngine(id: string, mode: 'destroy' | 'disconnect' = 'destroy'): Promise<void> {
    const engine = this.engines.get(id);
    if (!engine) return;
    this.engines.delete(id);
    try {
      await (mode === 'disconnect' ? engine.disconnect() : engine.destroy());
    } catch (error: unknown) {
      this.logger.warn(`Engine ${mode} failed (ignored)`, {
        sessionId: id,
        error: error instanceof Error ? error.message : String(error),
        action: 'engine_teardown_error',
      });
    }
  }

  private async clearAuthState(session: Session): Promise<void> {
    try {
      // A throwaway adapter is enough: constructing one has no side effects,
      // and clearAuthState only needs the session name to find its auth dir.
      await this.engineFactory.create({ sessionId: session.name }).clearAuthState();
    } catch (error: unknown) {
      this.recordError(session.id, error);
    }
  }

  private recordError(id: string, error: unknown): void {
    const message = error instanceof Error ? error.message : String(error);
    this.getRuntimeInfo(id).lastError = message;
    this.logger.error(`Session error: ${message}`, undefined, { sessionId: id, action: 'session_error' });
  }

  getRuntimeInfo(id: string): SessionRuntimeInfo {
    let info = this.runtime.get(id);
    if (!info) {
      info = { lastError: null, lastDisconnectReason: null, needsRelink: false };
      this.runtime.set(id, info);
    }
    return info;
  }

  async stop(id: string): Promise<Session> {
    const session = await this.findOne(id);

    // Cancel any reconnection attempts
    this.cancelReconnect(id);

    await this.teardownEngine(id, 'disconnect');

    this.logger.log(`Session stopped: ${session.name}`, {
      sessionId: id,
      action: 'stop',
    });
    await this.updateStatus(id, SessionStatus.DISCONNECTED);
    return this.findOne(id);
  }

  /**
   * Always resolves (no 400s) so pollers can render the real state: a QR when
   * one is ready, otherwise the status plus any error explaining why not.
   */
  async getQRCode(id: string): Promise<QRCodeState> {
    const session = await this.findOne(id);
    const engine = this.engines.get(id);
    const info = this.getRuntimeInfo(id);

    return {
      qrCode: engine?.getQRCode() ?? null,
      status: session.status,
      lastError: info.lastError,
      needsRelink: info.needsRelink,
    };
  }

  getEngine(id: string): IWhatsAppEngine | undefined {
    return this.engines.get(id);
  }

  async getGroups(id: string): Promise<{ id: string; name: string }[]> {
    await this.findOne(id); // Verify session exists
    const engine = this.engines.get(id);

    if (!engine) {
      throw new BadRequestException('Session is not started');
    }

    const groups = await engine.getGroups();
    return groups.map(g => ({
      id: g.id,
      name: g.name,
    }));
  }

  private async updateStatus(id: string, status: SessionStatus): Promise<void> {
    await this.sessionRepository.update(id, { status });
    this.logger.debug(`Session status updated to ${status}`, {
      sessionId: id,
      status,
      action: 'status_update',
    });
    // Emit real-time event to connected WebSocket clients
    this.eventsGateway.emitSessionStatus(id, status);
  }

  /**
   * Get overall session statistics for multi-session monitoring
   */
  async getStats(): Promise<{
    total: number;
    active: number;
    ready: number;
    disconnected: number;
    byStatus: Record<string, number>;
    memoryUsage: { heapUsed: number; heapTotal: number; rss: number };
  }> {
    const sessions = await this.findAll();
    const byStatus: Record<string, number> = {};

    for (const session of sessions) {
      byStatus[session.status] = (byStatus[session.status] || 0) + 1;
    }

    const memory = process.memoryUsage();

    return {
      total: sessions.length,
      active: this.engines.size,
      ready: byStatus[SessionStatus.READY] || 0,
      disconnected: byStatus[SessionStatus.DISCONNECTED] || 0,
      byStatus,
      memoryUsage: {
        heapUsed: Math.round(memory.heapUsed / 1024 / 1024),
        heapTotal: Math.round(memory.heapTotal / 1024 / 1024),
        rss: Math.round(memory.rss / 1024 / 1024),
      },
    };
  }

  /**
   * Get count of currently active (running) sessions
   */
  getActiveCount(): number {
    return this.engines.size;
  }

  /**
   * Check if session is currently active (engine running)
   */
  isActive(id: string): boolean {
    return this.engines.has(id);
  }
}
