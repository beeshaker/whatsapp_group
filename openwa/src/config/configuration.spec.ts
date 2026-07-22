import configuration from './configuration';

describe('configuration', () => {
  afterEach(() => {
    delete process.env.BAILEYS_AUTH_DIR;
  });

  it('defaults engine.baileysAuthDir to ./data/baileys when BAILEYS_AUTH_DIR is unset', () => {
    const config = configuration();
    expect(config.engine.baileysAuthDir).toBe('./data/baileys');
  });

  it('reads engine.baileysAuthDir from BAILEYS_AUTH_DIR when set', () => {
    process.env.BAILEYS_AUTH_DIR = '/app/data/baileys';
    const config = configuration();
    expect(config.engine.baileysAuthDir).toBe('/app/data/baileys');
  });

  it('does not change the existing engine.type default (whatsapp-web.js)', () => {
    const config = configuration();
    expect(config.engine.type).toBe('whatsapp-web.js');
  });
});
