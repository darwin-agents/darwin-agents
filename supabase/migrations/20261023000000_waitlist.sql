-- Landing-page waitlist. Browsers may insert; nobody can read rows back via the API.
create table if not exists public.waitlist (
  id         bigint generated always as identity primary key,
  email      text not null unique
             check (length(email) <= 254 and email ~* '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
  source     text,
  created_at timestamptz not null default now()
);

alter table public.waitlist enable row level security;

create policy "waitlist insert"
  on public.waitlist for insert
  to anon, authenticated
  with check (true);
