import { PluginContext, PluginType, IEnginePlugin } from '../../../core/plugins';
import { IWhatsAppEngine } from '../../../engine/interfaces/whatsapp-engine.interface';
import { BaileysAdapter } from '../../../engine/adapters/baileys.adapter';

export class BaileysPlugin implements IEnginePlugin {
  type = PluginType.ENGINE as const;
  private context?: PluginContext;

  onLoad(context: PluginContext): Promise<void> {
    this.context = context;
    context.logger.log('Baileys engine plugin loaded');
    return Promise.resolve();
  }

  onEnable(context: PluginContext): Promise<void> {
    context.logger.log('Baileys engine plugin enabled');
    return Promise.resolve();
  }

  onDisable(context: PluginContext): Promise<void> {
    context.logger.log('Baileys engine plugin disabled');
    return Promise.resolve();
  }

  createEngine(config: Record<string, unknown>): IWhatsAppEngine {
    const sessionId = config.sessionId as string;
    const authDir = this.resolveAuthDir();

    return new BaileysAdapter({ sessionId, authDir });
  }

  // EngineFactory.create() doesn't thread ConfigService values into
  // createEngine()'s config argument, and this.context.config only carries
  // dashboard-configured per-plugin overrides (empty unless someone has
  // explicitly set one there) — so read the env var directly as the
  // effective default, making BAILEYS_AUTH_DIR actually configurable.
  private resolveAuthDir(): string {
    const configured = this.context?.config.baileysAuthDir as string | undefined;
    return configured || process.env.BAILEYS_AUTH_DIR || './data/baileys';
  }

  getFeatures(): string[] {
    return ['text-messages', 'message-replies', 'message-reactions', 'group-management-read'];
  }

  healthCheck(): Promise<{ healthy: boolean; message?: string }> {
    return Promise.resolve({ healthy: true, message: 'Baileys engine is available' });
  }
}

export default BaileysPlugin;
