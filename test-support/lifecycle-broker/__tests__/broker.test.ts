import { validateRequest } from '../protocol';

describe('protocol validation', () => {
  const base = {
    action: 'stop',
    service: 'web',
    projectName: 'gp-abc123-1',
  };

  it('accepts valid requests', () => {
    expect(validateRequest(base)).toBeNull();
    expect(
      validateRequest({ ...base, action: 'kill', service: 'worker', signal: 'SIGKILL' })
    ).toBeNull();
    expect(
      validateRequest({ ...base, action: 'probe_egress', service: 'controlled-provider' })
    ).toBeNull();
  });

  it('rejects invalid project names', () => {
    expect(
      validateRequest({ ...base, projectName: 'bad-name' })
    ).toBe('invalid_project_name');
    expect(
      validateRequest({ ...base, projectName: '' })
    ).toBe('invalid_project_name');
    expect(
      validateRequest({ ...base, projectName: 'gp-ABC-1' })
    ).toBe('invalid_project_name');
  });

  it('rejects unknown actions', () => {
    expect(
      validateRequest({ ...base, action: 'destroy' })
    ).toBe('unknown_action');
  });

  it('rejects unknown services', () => {
    expect(
      validateRequest({ ...base, service: 'database' })
    ).toBe('unknown_service');
  });

  it('rejects controlled-provider for non-probe actions', () => {
    expect(
      validateRequest({ ...base, action: 'stop', service: 'controlled-provider' })
    ).toBe('unknown_service');
  });

  it('accepts controlled-provider for probe_egress', () => {
    expect(
      validateRequest({
        ...base,
        action: 'probe_egress',
        service: 'controlled-provider',
      })
    ).toBeNull();
  });

  it('rejects kill without SIGKILL signal', () => {
    expect(
      validateRequest({ ...base, action: 'kill', service: 'worker' })
    ).toBe('invalid_signal');
    expect(
      validateRequest({
        ...base,
        action: 'kill',
        service: 'worker',
        signal: 'SIGTERM',
      })
    ).toBe('invalid_signal');
  });

  it('accepts all runtime services', () => {
    for (const service of ['web', 'api', 'worker']) {
      expect(
        validateRequest({ ...base, service })
      ).toBeNull();
    }
  });

  it('accepts all valid actions', () => {
    for (const action of [
      'stop',
      'start',
      'restart',
      'status',
      'inspect_network',
    ]) {
      expect(
        validateRequest({ ...base, action })
      ).toBeNull();
    }
  });
});
