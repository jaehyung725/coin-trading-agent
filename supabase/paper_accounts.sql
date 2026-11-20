begin;

create table if not exists public.paper_account_snapshots (
    user_id uuid primary key references auth.users (id) on delete cascade,
    revision bigint not null default 1 check (revision > 0),
    snapshot text not null
);

alter table public.paper_account_snapshots enable row level security;
revoke all on public.paper_account_snapshots from public, anon, authenticated, service_role;
grant select, insert, update on public.paper_account_snapshots to service_role;

commit;
