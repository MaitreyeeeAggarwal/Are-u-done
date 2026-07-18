create table onboarding_state (
  user_id text primary key,
  stage text not null default 'not_started' check (stage in (
    'not_started','awaiting_yearly','awaiting_monthly_confirmation','awaiting_monthly_additions',
    'awaiting_weekly_confirmation','awaiting_weekly_additions','awaiting_daily_confirmation',
    'awaiting_daily_additions','complete'
  )),
  working_data jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

create or replace function set_onboarding_state(p_user_id text, p_stage text, p_working_data jsonb)
returns void language sql security definer as $$
  insert into onboarding_state(user_id,stage,working_data)
  values(p_user_id,p_stage,p_working_data)
  on conflict(user_id) do update set stage=excluded.stage, working_data=excluded.working_data, updated_at=now();
$$;

create or replace function create_active_goals(p_user_id text, p_level text, p_items jsonb)
returns setof goals language plpgsql security definer as $$
declare item jsonb; created goals;
begin
  for item in select * from jsonb_array_elements(p_items) loop
    insert into goals(user_id,title,level,target_date,status)
    values(p_user_id,item->>'title',p_level,nullif(item->>'target_date','')::date,'active')
    returning * into created;
    return next created;
  end loop;
end $$;

create or replace function create_proposed_children(p_user_id text, p_parent_goal_id uuid, p_level text, p_items jsonb)
returns setof goals language plpgsql security definer as $$
declare item jsonb; created goals;
begin
  for item in select * from jsonb_array_elements(p_items) loop
    insert into goals(user_id,title,level,parent_goal_id,target_date,status)
    values(p_user_id,item->>'title',p_level,p_parent_goal_id,nullif(item->>'target_date','')::date,'proposed')
    returning * into created;
    return next created;
  end loop;
end $$;

create or replace function activate_goal_ids(p_user_id text, p_goal_ids uuid[])
returns void language sql security definer as $$
  update goals set status='active' where user_id=p_user_id and id=any(p_goal_ids) and status='proposed';
$$;

create or replace function materialize_daily_tasks(p_user_id text, p_goal_ids uuid[])
returns void language plpgsql security definer as $$
declare g goals; due_time timestamptz;
begin
  for g in select * from goals where user_id=p_user_id and id=any(p_goal_ids) and level='daily' and status='active' loop
    due_time := coalesce(g.target_date::timestamptz + interval '19 hours', date_trunc('day', now()) + interval '19 hours');
    perform create_task_with_reminders(p_user_id,g.title,null,'Inbox',due_time,'normal','goal_cascade');
    update tasks set goal_id=g.id where id=(select id from tasks where user_id=p_user_id order by created_at desc limit 1);
  end loop;
end $$;
