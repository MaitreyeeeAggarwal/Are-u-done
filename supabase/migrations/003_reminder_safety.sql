-- Do not create retroactive reminders for a task added close to (or after) its due time.
create or replace function create_task_with_reminders(p_user_id text,p_title text,p_description text,p_list_name text,p_due_at timestamptz,p_priority text,p_source text)
returns setof tasks language plpgsql security definer as $$
declare v_list uuid; declare v_task tasks;
begin
  insert into lists(user_id,name) values(p_user_id,p_list_name)
  on conflict(user_id,name) do update set name=excluded.name returning id into v_list;
  insert into tasks(user_id,list_id,title,description,due_at,priority,source)
  values(p_user_id,v_list,p_title,p_description,p_due_at,p_priority,p_source) returning * into v_task;
  if p_due_at > now() + interval '24 hours' then
    insert into reminders(task_id,remind_at,kind) values(v_task.id,p_due_at-interval '24 hours','before_due');
  end if;
  if p_due_at > now() + interval '5 minutes' then
    insert into reminders(task_id,remind_at,kind) values(v_task.id,p_due_at,'due');
  end if;
  if p_source='urgent' then
    if greatest(now() + interval '5 minutes', date_trunc('day',now())+interval '19 hours') > now() + interval '5 minutes' then
      insert into reminders(task_id,remind_at,kind) values(v_task.id,greatest(now() + interval '5 minutes',date_trunc('day',now())+interval '19 hours'),'urgent_evening');
    end if;
    if date_trunc('day',p_due_at)+interval '9 hours' > now() + interval '5 minutes' then
      insert into reminders(task_id,remind_at,kind) values(v_task.id,date_trunc('day',p_due_at)+interval '9 hours','urgent_morning');
    end if;
  end if;
  return next v_task;
end $$;

-- A daily item created late in the day gets a future due time, never a past one.
create or replace function materialize_daily_tasks(p_user_id text, p_goal_ids uuid[])
returns void language plpgsql security definer as $$
declare g goals; due_time timestamptz;
begin
  for g in select * from goals where user_id=p_user_id and id=any(p_goal_ids) and level='daily' and status='active' loop
    due_time := coalesce(g.target_date::timestamptz + interval '19 hours', greatest(now() + interval '1 hour', date_trunc('day',now()) + interval '19 hours'));
    perform create_task_with_reminders(p_user_id,g.title,null,'Inbox',due_time,'normal','goal_cascade');
    update tasks set goal_id=g.id where id=(select id from tasks where user_id=p_user_id order by created_at desc limit 1);
  end loop;
end $$;

-- Sandbox-safe delivery rate: no more than three automated reminders per five-minute poll.
create or replace function queue_due_reminders(p_worker_id text) returns void language plpgsql security definer as $$
declare r record;
begin
  for r in
    select rem.id, t.user_id, t.title
    from reminders rem join tasks t on t.id=rem.task_id
    where rem.status='pending' and rem.remind_at<=now() and t.status in ('todo','doing')
    order by rem.remind_at
    limit 3
    for update of rem skip locked
  loop
    insert into outbox(user_id,to_number,body,kind,reminder_id)
    values(r.user_id,r.user_id,'Reminder: '||r.title,'reminder',r.id);
    update reminders set status='sent' where id=r.id;
  end loop;
end $$;

-- Twilio error 63038 is an account-wide daily quota, not a permanent delivery
-- failure. Keep a non-reminder reply retryable instead of exhausting its retry
-- budget while the quota is unavailable. Reminders are rate-limited above.
create or replace function fail_outbox(p_outbox_id uuid,p_worker_id text,p_error text,p_max_attempts int)
returns void language plpgsql security definer as $$
declare v_reminder_id uuid;
declare v_status text;
begin
  if p_error like '%63038%' then
    update outbox
    set status='pending',
        next_attempt_at=now()+interval '1 hour',
        lease_owner=null,
        lease_until=null,
        failed_reason=p_error
    where id=p_outbox_id and lease_owner=p_worker_id;
    return;
  end if;

  update outbox
  set attempts=attempts+1,
      status=case when attempts+1>=p_max_attempts then 'failed' else 'pending' end,
      next_attempt_at=now()+make_interval(secs=>least(3600,60*power(2,attempts)::int)),
      lease_owner=null,
      lease_until=null,
      failed_reason=p_error
  where id=p_outbox_id and lease_owner=p_worker_id
  returning reminder_id, status into v_reminder_id, v_status;

  if v_status='failed' and v_reminder_id is not null then
    update reminders set status='failed', failed_reason=p_error where id=v_reminder_id;
  end if;
end $$;
