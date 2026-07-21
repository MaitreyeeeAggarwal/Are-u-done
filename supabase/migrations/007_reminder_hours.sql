-- Never send automated reminders outside the user’s local 10:00–22:00 window.
create or replace function queue_due_reminders(p_worker_id text, p_timezone text)
returns void language plpgsql security definer as $$
declare r record;
begin
  if extract(hour from now() at time zone p_timezone) < 10 or extract(hour from now() at time zone p_timezone) >= 22 then
    return;
  end if;
  for r in
    select rem.id, t.user_id, t.title
    from reminders rem join tasks t on t.id = rem.task_id
    where rem.status = 'pending' and rem.remind_at <= now()
      and t.status in ('todo', 'doing') and t.blocked_at is null
    order by rem.remind_at
    limit 3
    for update of rem skip locked
  loop
    insert into outbox(user_id,to_number,body,kind,reminder_id)
    values(r.user_id,r.user_id,'Reminder: ' || r.title,'reminder',r.id);
    update reminders set status = 'sent' where id = r.id;
  end loop;
end;
$$;