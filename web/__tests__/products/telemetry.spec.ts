/**
 * @jest-environment jsdom
 */
import {
  emitProductIntakeEvent,
  sanitizeProductIntakeEvent,
} from '@/lib/products/telemetry';

describe('product intake telemetry', () => {
  afterEach(() => {
    delete (window as any).__PRODUCT_TELEMETRY__;
  });

  it('emits the three events with bounded labels only', () => {
    const sink = jest.fn();
    (window as any).__PRODUCT_TELEMETRY__ = sink;

    emitProductIntakeEvent({
      event: 'product_intake_started',
      inputKinds: ['text', 'url', 'text'],
      replayed: false,
    });
    emitProductIntakeEvent({
      event: 'product_intake_failed',
      stage: 'intake',
      failureCode: 'access_denied',
      suggestedAction: 'paste_text_or_screenshot',
      executionMode: 'mock',
    });
    emitProductIntakeEvent({
      event: 'product_intake_confirmed',
      executionMode: 'byok',
      durationMs: 1234.6,
    });

    expect(sink.mock.calls.map(([event]) => event)).toEqual([
      {
        event: 'product_intake_started',
        inputKinds: ['text', 'url'],
        replayed: false,
      },
      {
        event: 'product_intake_failed',
        stage: 'intake',
        failureCode: 'access_denied',
        suggestedAction: 'paste_text_or_screenshot',
        executionMode: 'mock',
      },
      {
        event: 'product_intake_confirmed',
        executionMode: 'byok',
        durationMs: 1235,
      },
    ]);
  });

  it('collapses anything that could carry an id, url or free text', () => {
    expect(
      sanitizeProductIntakeEvent({
        event: 'product_intake_failed',
        failureCode: 'https://shop.example/p?id=prod_123',
        suggestedAction: 'call me',
        executionMode: 'prod_1',
      })
    ).toEqual({
      event: 'product_intake_failed',
      failureCode: 'unknown',
      suggestedAction: 'unknown',
      executionMode: 'unknown',
    });
    expect(
      sanitizeProductIntakeEvent({ event: 'something_else' as any })
    ).toBeNull();
  });

  it('never throws when the sink does', () => {
    (window as any).__PRODUCT_TELEMETRY__ = () => {
      throw new Error('sink down');
    };
    expect(() =>
      emitProductIntakeEvent({ event: 'product_intake_started' })
    ).not.toThrow();
  });
});
