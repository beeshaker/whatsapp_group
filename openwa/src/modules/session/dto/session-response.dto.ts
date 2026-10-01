import { ApiProperty, ApiPropertyOptional } from '@nestjs/swagger';
import { SessionStatus } from '../entities/session.entity';

export class SessionResponseDto {
  @ApiProperty({ example: 'sess_123e4567-e89b-12d3-a456-426614174000' })
  id: string;

  @ApiProperty({ example: 'my-bot' })
  name: string;

  @ApiProperty({ enum: SessionStatus, example: SessionStatus.READY })
  status: SessionStatus;

  @ApiPropertyOptional({ example: '628123456789' })
  phone?: string | null;

  @ApiPropertyOptional({ example: 'John Doe' })
  pushName?: string | null;

  @ApiPropertyOptional({ example: '2025-02-02T10:00:00Z' })
  connectedAt?: Date | null;

  @ApiPropertyOptional({ example: '2025-02-02T10:30:00Z' })
  lastActive?: Date | null;

  @ApiPropertyOptional({ description: 'Most recent start/connection error, if any', example: null })
  lastError?: string | null;

  @ApiPropertyOptional({ description: 'Reason given for the most recent disconnect', example: 'LOGOUT' })
  lastDisconnectReason?: string | null;

  @ApiPropertyOptional({ description: 'WhatsApp unlinked this device; a fresh QR must be scanned' })
  needsRelink?: boolean;

  @ApiProperty({ example: '2025-02-02T09:00:00Z' })
  createdAt: Date;

  @ApiProperty({ example: '2025-02-02T10:00:00Z' })
  updatedAt: Date;
}

export class QRCodeResponseDto {
  @ApiProperty({
    description: 'QR code as data URL, or null when no QR is currently available',
    example: 'data:image/png;base64,...',
    nullable: true,
  })
  qrCode: string | null;

  @ApiProperty({ enum: SessionStatus, example: SessionStatus.QR_READY })
  status: SessionStatus;

  @ApiPropertyOptional({ nullable: true })
  lastError: string | null;

  @ApiProperty()
  needsRelink: boolean;
}
