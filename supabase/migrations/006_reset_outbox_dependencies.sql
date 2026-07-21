create or replace function reset_generated_goals(p_user_id text)
returns table(deleted_task_count integer)
language plpgsql security definer as $$
declare task_count integer;
begin
  select count(*)::integer into task_count from tasks where user_id = p_user_id and source = 'goal_cascade';
  delete from task_list_snapshots where user_id = p_user_id;
  delete from outbox where reminder_id in (
    select r.id from reminders r join tasks t on t.id = r.task_id
    where t.user_id = p_user_id and t.source = 'goal_cascade'
  );
  delete from tasks where user_id = p_user_id and source = 'goal_cascade';
  delete from goals where user_id = p_user_id;
  delete from onboarding_state where user_id = p_user_id;
  delete from conversation_state where user_id = p_user_id;
  return query select task_count;
end;
$$;