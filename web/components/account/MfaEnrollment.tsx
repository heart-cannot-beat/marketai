import Link from 'next/link';
import { useRouter } from 'next/router';
import { useEffect, useState, type FormEvent } from 'react';
import { Button } from 'react-daisyui';
import { useTranslation } from 'next-i18next';

import { Card } from '@/components/shared';
import { defaultHeaders } from '@/lib/common';

type Enrollment = { secret: string; otpauthUri: string };
type Status = { enrolled: boolean; remainingRecoveryCodes: number };

async function responseData<T>(response: Response): Promise<T> {
  const body = (await response.json()) as {
    data?: T;
    error?: { message?: string };
  };
  if (!response.ok || !body.data) {
    throw new Error(body.error?.message ?? 'MFA setup failed.');
  }
  return body.data;
}

/** Authenticator enrollment, including the one-time recovery-code handoff. */
export default function MfaEnrollment() {
  const { t } = useTranslation('common');
  const router = useRouter();
  const onboardingSlug =
    typeof router.query.onboarding === 'string'
      ? router.query.onboarding
      : null;
  const [status, setStatus] = useState<Status | null>(null);
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [code, setCode] = useState('');
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    let active = true;
    void fetch('/api/auth/mfa')
      .then((response) => responseData<Status>(response))
      .then((data) => {
        if (active) setStatus(data);
      })
      .catch((reason: unknown) => {
        if (active)
          setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      active = false;
    };
  }, []);

  async function begin() {
    setPending(true);
    setError(null);
    try {
      const response = await fetch('/api/auth/mfa', {
        method: 'POST',
        headers: defaultHeaders,
        body: JSON.stringify({ action: 'begin' }),
      });
      setEnrollment(await responseData<Enrollment>(response));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setPending(false);
    }
  }

  async function confirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const response = await fetch('/api/auth/mfa', {
        method: 'POST',
        headers: defaultHeaders,
        body: JSON.stringify({ action: 'confirm', code }),
      });
      const result = await responseData<{ recoveryCodes: string[] }>(response);
      setRecoveryCodes(result.recoveryCodes);
      setStatus({
        enrolled: true,
        remainingRecoveryCodes: result.recoveryCodes.length,
      });
      setEnrollment(null);
      setCode('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setPending(false);
    }
  }

  return (
    <Card>
      <Card.Body>
        <Card.Header>
          <Card.Title>{t('onboarding-mfa-begin')}</Card.Title>
          <Card.Description>{t('onboarding-mfa-scan')}</Card.Description>
        </Card.Header>

        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}

        {status === null && !error && (
          <span className="loading loading-spinner" aria-label="Loading MFA" />
        )}

        {status && !status.enrolled && !enrollment && (
          <Button color="primary" loading={pending} onClick={begin}>
            {t('onboarding-mfa-begin')}
          </Button>
        )}

        {enrollment && (
          <form className="space-y-4" onSubmit={confirm}>
            <div>
              <p className="font-medium">{t('onboarding-mfa-secret')}</p>
              <code className="block break-all rounded bg-base-200 p-3">
                {enrollment.secret}
              </code>
              <a
                className="link link-primary mt-2 inline-block break-all"
                href={enrollment.otpauthUri}
              >
                {t('onboarding-mfa-open-authenticator')}
              </a>
            </div>
            <label className="form-control" htmlFor="mfa-enrollment-code">
              <span className="label-text mb-1">
                {t('onboarding-mfa-code')}
              </span>
              <input
                id="mfa-enrollment-code"
                className="input input-bordered"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6}"
                required
                value={code}
                onChange={(event) => setCode(event.target.value)}
              />
            </label>
            <Button
              type="submit"
              color="primary"
              loading={pending}
              disabled={!/^\d{6}$/.test(code)}
            >
              {t('onboarding-mfa-confirm')}
            </Button>
          </form>
        )}

        {recoveryCodes.length > 0 && (
          <div className="space-y-3" aria-live="polite">
            <p className="font-semibold">{t('onboarding-mfa-save-recovery')}</p>
            <ul className="grid gap-1 rounded bg-base-200 p-4 font-mono sm:grid-cols-2">
              {recoveryCodes.map((recoveryCode) => (
                <li key={recoveryCode}>{recoveryCode}</li>
              ))}
            </ul>
          </div>
        )}

        {status?.enrolled && recoveryCodes.length === 0 && (
          <p>
            {t('onboarding-mfa-enabled', {
              count: status.remainingRecoveryCodes,
            })}
          </p>
        )}

        {status?.enrolled && onboardingSlug && (
          <Link
            className="btn btn-primary mt-4"
            href={`/w/${encodeURIComponent(onboardingSlug)}/experiments`}
          >
            {t('onboarding-continue-workspace')}
          </Link>
        )}
      </Card.Body>
    </Card>
  );
}
