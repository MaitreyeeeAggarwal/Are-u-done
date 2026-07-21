alter table tasks add column if not exists blocked_reason text;
alter table tasks add column if not exists blocked_at timestamptz;
create table if not exists task_events (
  id uuid primary key default gen_random_uuid(), task_id uuid not null references tasks(id) on delete cascade,
  user_id text not null, event_type text not null check(event_type in ('blocked','unblocked','completed','rescheduled')),
  details jsonb not null default '{}'::jsonb, created_at timestamptz not null default now()
);
create index if not exists task_events_task_created_idx on task_events(task_id, created_at desc);

create or replace function queue_due_reminders(p_worker_id text) returns void language plpgsql security definer as $$
declare r record;
begin
  for r in select rem.id, t.user_id, t.title from reminders rem join tasks t on t.id=rem.task_id
    where rem.status='pending' and rem.remind_at<=now() and t.status in ('todo','doing') and t.blocked_at is null
    order by rem.remind_at limit 3 for update of rem skip locked
  loop
    insert into outbox(user_id,to_number,body,kind,reminder_id) values(r.user_id,r.user_id,'Reminder: '||r.title,'reminder',r.id);
    update reminders set status='sent' where id=r.id;
  end loop;
end $$;