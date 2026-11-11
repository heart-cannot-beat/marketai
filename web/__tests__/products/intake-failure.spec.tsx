/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { IntakeFailure } from '@/components/products/IntakeFailure';
import { IntakeProgress } from '@/components/products/IntakeProgress';
import type { IntakeDetail } from '@/lib/products/intake-model';

function failed(
  code: string,
  action: string,
  status = 'fetch_failed'
): IntakeDetail {
  return {
    intake_id: 'pi_1',
    status,
    execution_mode: 'byok',
    input: { url: 'https://shop.example/p', text_chars: 0, upload_count: 0 },
    failure: {
      code,
      step: status === 'fetch_failed' ? 'fetch' : 'extract',
      message: '服务端原话',
      suggested_action: action,
      retryable: action === 'retry',
    },
    replayed: false,
  };
}

function renderFailure(detail: IntakeDetail, isOwner = true) {
  const onResubmit = jest.fn();
  render(
    <IntakeFailure
      detail={detail}
      slug="acme"
      isOwner={isOwner}
      lastInput={{ raw: 'https://shop.example/p', files: [] }}
      busy={false}
      onResubmit={onResubmit}
      onStartOver={jest.fn()}
    />
  );
  return onResubmit;
}

describe('IntakeFailure', () => {
  it('asks for a description or screenshots when the site cannot be read', () => {
    const onResubmit = renderFailure(
      failed('access_denied', 'paste_text_or_screenshot')
    );
    expect(screen.getByTestId('intake-failure-guidance')).toHaveTextContent(
      'Copy the product description below or upload a few page screenshots'
    );
    // The reason reads by the failure's code (#253), never the server text.
    expect(
      screen.getByText(
        'The site refused automated reading (it may need sign-in or block bots).'
      )
    ).toBeInTheDocument();
    expect(screen.queryByText('服务端原话')).toBeNull();

    const box = screen.getByLabelText('More description or screenshots');
    fireEvent.change(box, {
      target: { value: 'https://shop.example/p 白色壁挂充电桩，11 kW' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Read again with the additions' })
    );
    expect(onResubmit).toHaveBeenCalledWith({
      raw: 'https://shop.example/p 白色壁挂充电桩，11 kW',
      files: [],
    });
  });

  it('asks to check the link', () => {
    renderFailure(failed('not_found', 'check_link'));
    expect(screen.getByTestId('intake-failure-guidance')).toHaveTextContent(
      'The link could not be opened or is not valid'
    );
  });

  it('asks for more detail when the model could not build a card', () => {
    renderFailure(
      failed('extraction_invalid', 'add_more_detail', 'extraction_failed')
    );
    expect(screen.getByTestId('intake-failure-guidance')).toHaveTextContent(
      'Add a few lines: what the product is, who it is for'
    );
  });

  it('offers a plain retry for transient failures', () => {
    const onResubmit = renderFailure(
      failed('rate_limited', 'retry', 'extraction_failed')
    );
    expect(
      screen.queryByLabelText('More description or screenshots')
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Read again' }));
    expect(onResubmit).toHaveBeenCalledWith({
      raw: 'https://shop.example/p',
      files: [],
    });
  });

  it('sends the Owner to Settings to bind the OpenAI key', () => {
    renderFailure(
      failed('credential_unavailable', 'bind_openai_key', 'extraction_failed')
    );
    expect(
      screen.getByRole('link', { name: 'Bind an OpenAI key in Settings' })
    ).toHaveAttribute('href', '/w/acme/settings');
  });

  it('tells a Member to ask the Owner instead of linking to an Owner-only page', () => {
    renderFailure(
      failed('credential_unavailable', 'bind_openai_key', 'extraction_failed'),
      false
    );
    expect(screen.getByTestId('intake-failure-guidance')).toHaveTextContent(
      'Ask the workspace Owner to bind it in Settings'
    );
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it("lands the Owner on Settings' spending-limits section", () => {
    renderFailure(
      failed('guardrail_exceeded', 'adjust_spend_guardrail', 'failed')
    );
    expect(
      screen.getByRole('link', {
        name: 'Adjust the spending guardrail in Settings',
      })
    ).toHaveAttribute('href', '/w/acme/settings#spend-guardrail');
  });
});

describe('IntakeProgress', () => {
  it('shows each step state in words, and the mock badge', () => {
    render(
      <IntakeProgress
        detail={{
          intake_id: 'pi_1',
          status: 'extracting',
          execution_mode: 'mock',
          input: { url: null, text_chars: 20, upload_count: 0 },
          replayed: false,
        }}
        stalled={false}
        onStartOver={jest.fn()}
        onKeepWaiting={jest.fn()}
      />
    );
    expect(screen.getByTestId('intake-step-fetch')).toHaveTextContent(
      'Fetch the pageNo page to fetch'
    );
    expect(screen.getByTestId('intake-step-extract')).toHaveTextContent(
      'Read the detailsIn progress'
    );
    expect(screen.getByTestId('intake-step-images')).toHaveTextContent(
      'Process imagesWaiting'
    );
    expect(screen.getByTestId('intake-mock-badge')).toBeInTheDocument();
  });

  it('offers to keep waiting once polling gave up', () => {
    const onKeepWaiting = jest.fn();
    render(
      <IntakeProgress
        detail={null}
        stalled
        onStartOver={jest.fn()}
        onKeepWaiting={onKeepWaiting}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: 'Keep waiting' }));
    expect(onKeepWaiting).toHaveBeenCalled();
    expect(screen.queryByTestId('intake-mock-badge')).not.toBeInTheDocument();
  });
});
