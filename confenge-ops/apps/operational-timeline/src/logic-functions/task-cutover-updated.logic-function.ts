import {
  defineLogicFunction,
  type DatabaseEventPayload,
  type ObjectRecordUpdateEvent,
} from 'twenty-sdk/define';

import {
  isTaskSnapshot,
  isCutoverEnabled,
  loadCutoverRuntime,
  reconcileUpdatedEvent,
  type TaskSnapshot,
} from 'src/logic-functions/task-cutover';

export const WATCHED_TASK_FIELDS = [
  'status',
  'clickUpStatus',
  'clickUpStartOn',
  'clickUpStartAt',
  'clickUpDueOn',
  'dueAt',
] as const;

export const handleTaskUpdated = async (
  event: DatabaseEventPayload<ObjectRecordUpdateEvent<TaskSnapshot>>,
) => {
  if (!isCutoverEnabled()) {
    return { kind: 'disabled', applied: false };
  }

  const task = event.properties.after;

  if (!isTaskSnapshot(task)) {
    throw new Error('O evento tasks.updated não contém uma tarefa válida.');
  }

  const runtime = loadCutoverRuntime();
  return reconcileUpdatedEvent(
    task,
    event.properties.updatedFields,
    runtime,
    undefined,
    undefined,
    event.properties.before,
  );
};

export default defineLogicFunction({
  universalIdentifier: '0dca05ee-7131-4540-85b8-61ffff9944f5',
  name: 'task-cutover-updated',
  description:
    'Mantém Etapa, situação geral e semântica de datas coerentes após edições nativas.',
  timeoutSeconds: 10,
  databaseEventTriggerSettings: {
    eventName: 'tasks.updated',
    updatedFields: [...WATCHED_TASK_FIELDS],
  },
  handler: handleTaskUpdated,
});
