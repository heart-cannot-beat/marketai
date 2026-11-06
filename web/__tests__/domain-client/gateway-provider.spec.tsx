import { renderToString } from 'react-dom/server';
import {
  DomainGatewayProvider,
  useDomainGateway,
} from '@/lib/domain-client/gateway-provider';

function GatewayConsumer() {
  const gateway = useDomainGateway();
  return <span>{gateway ? 'gateway-ready' : 'gateway-missing'}</span>;
}

describe('DomainGatewayProvider', () => {
  it('is safe to render in a Pages Router server pass', () => {
    expect(() =>
      renderToString(
        <DomainGatewayProvider>
          <GatewayConsumer />
        </DomainGatewayProvider>
      )
    ).not.toThrow();
  });
});
