import { Role } from '@prisma/client';
import { availableRoles, customerRoles, isCustomerRole } from '@/lib/roles';
import { permissions } from '@/lib/permissions';
import { assertCustomerRole } from '@/lib/rbac';
import { updateMemberSchema, inviteViaEmailSchema } from '@/lib/zod';

describe('Customer role contract', () => {
  it('exposes Owner and Member only', () => {
    expect([...customerRoles].sort()).toEqual(['MEMBER', 'OWNER']);
    expect(availableRoles.map((r) => r.id).sort()).toEqual(['MEMBER', 'OWNER']);
  });

  it('does not carry the upstream ADMIN role in the Prisma enum', () => {
    expect(Object.keys(Role).sort()).toEqual(['MEMBER', 'OWNER']);
    expect((Role as Record<string, string>).ADMIN).toBeUndefined();
  });

  it('declares permissions for exactly the contract roles', () => {
    expect(Object.keys(permissions).sort()).toEqual(['MEMBER', 'OWNER']);
  });

  it('does not grant a Member any write permission', () => {
    const memberActions = permissions.MEMBER.flatMap((p) =>
      p.actions === '*' ? ['*'] : p.actions
    );

    expect(memberActions).toEqual(expect.arrayContaining(['read', 'leave']));
    expect(memberActions).not.toContain('*');
    expect(memberActions).not.toContain('create');
    expect(memberActions).not.toContain('update');
    expect(memberActions).not.toContain('delete');
    expect(permissions.MEMBER.map((p) => p.resource)).toEqual(['team']);
  });

  it('rejects ADMIN at the guard', () => {
    expect(isCustomerRole('ADMIN')).toBe(false);
    expect(() => assertCustomerRole('ADMIN')).toThrow(/OWNER or MEMBER/);
    expect(() => assertCustomerRole(Role.OWNER)).not.toThrow();
    expect(() => assertCustomerRole(Role.MEMBER)).not.toThrow();
  });

  it('rejects ADMIN in the membership update request schema', () => {
    expect(
      updateMemberSchema.safeParse({ memberId: 'u1', role: 'ADMIN' }).success
    ).toBe(false);
    expect(
      updateMemberSchema.safeParse({ memberId: 'u1', role: 'OWNER' }).success
    ).toBe(true);
  });

  it('rejects ADMIN in the invitation request schema', () => {
    expect(
      inviteViaEmailSchema.safeParse({
        email: 'someone@example.com',
        role: 'ADMIN',
      }).success
    ).toBe(false);

    expect(
      inviteViaEmailSchema.safeParse({
        email: 'someone@example.com',
        role: 'MEMBER',
      }).success
    ).toBe(true);
  });
});
