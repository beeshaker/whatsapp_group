import {
  Controller,
  Get,
  Post,
  Delete,
  Param,
  Body,
  Query,
  HttpCode,
  HttpStatus,
  BadRequestException,
} from '@nestjs/common';
import { ApiTags, ApiOperation, ApiResponse, ApiParam, ApiQuery } from '@nestjs/swagger';
import { SessionService } from './session.service';
import { CreateSessionDto, SessionResponseDto, QRCodeResponseDto } from './dto';
import { Session } from './entities/session.entity';
import { AuditService } from '../audit/audit.service';
import { AuditAction } from '../audit/entities/audit-log.entity';
import { RequireRole } from '../auth/decorators/auth.decorators';
import { IncomingMessage } from '../../engine/interfaces/whatsapp-engine.interface';
import { ApiKeyRole } from '../auth/entities/api-key.entity';

@ApiTags('sessions')
@Controller('sessions')
export class SessionController {
  constructor(
    private readonly sessionService: SessionService,
    private readonly auditService: AuditService,
  ) {}

  // Transform entity to DTO with lastActive field name
  private transformSession(session: Session): SessionResponseDto {
    return {
      id: session.id,
      name: session.name,
      status: session.status,
      phone: session.phone,
      pushName: session.pushName,
      connectedAt: session.connectedAt,
      lastActive: session.lastActiveAt,
      ...this.sessionService.getRuntimeInfo(session.id),
      createdAt: session.createdAt,
      updatedAt: session.updatedAt,
    };
  }

  @Post()
  @RequireRole(ApiKeyRole.OPERATOR)
  @ApiOperation({ summary: 'Create a new WhatsApp session' })
  @ApiResponse({
    status: 201,
    description: 'Session created',
    type: SessionResponseDto,
  })
  @ApiResponse({ status: 409, description: 'Session name already exists' })
  async create(@Body() dto: CreateSessionDto): Promise<Session> {
    const session = await this.sessionService.create(dto);
    await this.auditService.logInfo(AuditAction.SESSION_CREATED, {
      sessionId: session.id,
      sessionName: session.name,
    });
    return session;
  }

  @Get()
  @ApiOperation({ summary: 'List all sessions' })
  @ApiResponse({
    status: 200,
    description: 'List of sessions',
    type: [SessionResponseDto],
  })
  async findAll(): Promise<SessionResponseDto[]> {
    const sessions = await this.sessionService.findAll();
    return sessions.map(s => this.transformSession(s));
  }

  @Get(':id')
  @ApiOperation({ summary: 'Get session by ID' })
  @ApiParam({ name: 'id', description: 'Session ID' })
  @ApiResponse({
    status: 200,
    description: 'Session details',
    type: SessionResponseDto,
  })
  @ApiResponse({ status: 404, description: 'Session not found' })
  async findOne(@Param('id') id: string): Promise<SessionResponseDto> {
    const session = await this.sessionService.findOne(id);
    return this.transformSession(session);
  }

  @Delete(':id')
  @RequireRole(ApiKeyRole.OPERATOR)
  @HttpCode(HttpStatus.NO_CONTENT)
  @ApiOperation({ summary: 'Delete a session' })
  @ApiParam({ name: 'id', description: 'Session ID' })
  @ApiResponse({ status: 204, description: 'Session deleted' })
  @ApiResponse({ status: 404, description: 'Session not found' })
  async delete(@Param('id') id: string): Promise<void> {
    const session = await this.sessionService.findOne(id);
    await this.sessionService.delete(id);
    await this.auditService.logInfo(AuditAction.SESSION_DELETED, {
      sessionId: id,
      sessionName: session.name,
    });
  }

  @Post(':id/start')
  @RequireRole(ApiKeyRole.OPERATOR)
  @ApiOperation({
    summary: 'Start a session and initialize WhatsApp connection',
  })
  @ApiParam({ name: 'id', description: 'Session ID' })
  @ApiResponse({
    status: 200,
    description: 'Session started',
    type: SessionResponseDto,
  })
  @ApiResponse({ status: 400, description: 'Session already started' })
  @ApiResponse({ status: 404, description: 'Session not found' })
  async start(@Param('id') id: string): Promise<SessionResponseDto> {
    const session = await this.sessionService.start(id);
    await this.auditService.logInfo(AuditAction.SESSION_STARTED, {
      sessionId: session.id,
      sessionName: session.name,
    });
    return this.transformSession(session);
  }

  @Post(':id/stop')
  @RequireRole(ApiKeyRole.OPERATOR)
  @ApiOperation({ summary: 'Stop a session and disconnect WhatsApp' })
  @ApiParam({ name: 'id', description: 'Session ID' })
  @ApiResponse({
    status: 200,
    description: 'Session stopped',
    type: SessionResponseDto,
  })
  @ApiResponse({ status: 404, description: 'Session not found' })
  async stop(@Param('id') id: string): Promise<SessionResponseDto> {
    const session = await this.sessionService.stop(id);
    await this.auditService.logInfo(AuditAction.SESSION_STOPPED, {
      sessionId: session.id,
      sessionName: session.name,
    });
    return this.transformSession(session);
  }

  @Post(':id/restart')
  @RequireRole(ApiKeyRole.OPERATOR)
  @ApiOperation({
    summary: 'Restart the connection, keeping the saved login (no QR scan needed). Works in any state.',
  })
  @ApiParam({ name: 'id', description: 'Session ID' })
  @ApiResponse({ status: 200, description: 'Session restarting', type: SessionResponseDto })
  @ApiResponse({ status: 404, description: 'Session not found' })
  async restart(@Param('id') id: string): Promise<SessionResponseDto> {
    const session = await this.sessionService.restart(id);
    await this.auditService.logInfo(AuditAction.SESSION_STARTED, {
      sessionId: session.id,
      sessionName: session.name,
      metadata: { mode: 'restart' },
    });
    return this.transformSession(session);
  }

  @Post(':id/relink')
  @RequireRole(ApiKeyRole.OPERATOR)
  @ApiOperation({
    summary: 'Wipe the saved login and start fresh so a new QR code is shown. Works in any state.',
  })
  @ApiParam({ name: 'id', description: 'Session ID' })
  @ApiResponse({ status: 200, description: 'Session re-linking', type: SessionResponseDto })
  @ApiResponse({ status: 404, description: 'Session not found' })
  async relink(@Param('id') id: string): Promise<SessionResponseDto> {
    const session = await this.sessionService.relink(id);
    await this.auditService.logInfo(AuditAction.SESSION_STARTED, {
      sessionId: session.id,
      sessionName: session.name,
      metadata: { mode: 'relink' },
    });
    return this.transformSession(session);
  }

  @Get(':id/qr')
  @ApiOperation({ summary: 'Get QR code for session authentication' })
  @ApiParam({ name: 'id', description: 'Session ID' })
  @ApiResponse({
    status: 200,
    description: 'QR code data',
    type: QRCodeResponseDto,
  })
  @ApiResponse({ status: 404, description: 'Session not found' })
  async getQRCode(@Param('id') id: string): Promise<QRCodeResponseDto> {
    const qrState = await this.sessionService.getQRCode(id);
    if (qrState.qrCode) {
      await this.auditService.logInfo(AuditAction.SESSION_QR_GENERATED, {
        sessionId: id,
      });
    }
    return qrState;
  }

  @Get(':id/history')
  @RequireRole(ApiKeyRole.OPERATOR)
  @ApiOperation({
    summary: 'Past messages captured from the link-time history sync (Baileys), oldest first. For one-off backfills.',
  })
  @ApiParam({ name: 'id', description: 'Session ID' })
  @ApiQuery({ name: 'chatId', required: false })
  @ApiQuery({ name: 'since', required: false, type: Number, description: 'Epoch seconds, inclusive' })
  @ApiQuery({ name: 'until', required: false, type: Number, description: 'Epoch seconds, inclusive' })
  @ApiResponse({ status: 200, description: 'Messages in the engine-neutral IncomingMessage shape' })
  @ApiResponse({ status: 400, description: 'Engine does not support history, or bad since/until' })
  async getHistory(
    @Param('id') id: string,
    @Query('chatId') chatId?: string,
    @Query('since') since?: string,
    @Query('until') until?: string,
  ): Promise<IncomingMessage[]> {
    const toEpoch = (value: string | undefined, name: string): number | undefined => {
      if (value === undefined || value === '') return undefined;
      const n = Number(value);
      if (!Number.isFinite(n)) throw new BadRequestException(`${name} must be epoch seconds`);
      return n;
    };
    return this.sessionService.getHistory(id, {
      chatId: chatId || undefined,
      since: toEpoch(since, 'since'),
      until: toEpoch(until, 'until'),
    });
  }

  @Get(':id/groups')
  @ApiOperation({ summary: 'Get all groups for a session' })
  @ApiParam({ name: 'id', description: 'Session ID' })
  @ApiResponse({
    status: 200,
    description: 'List of groups the session is a member of',
  })
  @ApiResponse({ status: 400, description: 'Session not ready' })
  @ApiResponse({ status: 404, description: 'Session not found' })
  async getGroups(@Param('id') id: string): Promise<{ id: string; name: string }[]> {
    return this.sessionService.getGroups(id);
  }

  @Get('stats/overview')
  @ApiOperation({
    summary: 'Get session statistics for multi-session monitoring',
  })
  @ApiResponse({
    status: 200,
    description: 'Session statistics including counts and memory usage',
  })
  async getStats(): Promise<{
    total: number;
    active: number;
    ready: number;
    disconnected: number;
    byStatus: Record<string, number>;
    memoryUsage: { heapUsed: number; heapTotal: number; rss: number };
  }> {
    return this.sessionService.getStats();
  }
}
