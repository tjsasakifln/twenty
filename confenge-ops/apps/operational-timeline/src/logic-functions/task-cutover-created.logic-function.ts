import {
  defineLogicFunction,
  STANDARD_OBJECT,
  type DatabaseEventPayload,
  type ObjectRecordCreateEvent,
} from 'twenty-sdk/define';

import {
  isTaskSnapshot,
  isCutoverEnabled,
  loadCutoverRuntime,
  reconcileCreatedEvent,
  type TaskSnapshot,
} from 'src/logic-functions/task-cutover';

export const handleTaskCreated = async (
  event: DatabaseEventPayload<ObjectRecordCreateEvent<TaskSnapshot>>,
) => {
  if (!isCutoverEnabled()) {
    return { kind: 'disabled', applied: false };
  }

  const task = event.properties.after;

  if (!isTaskSnapshot(task)) {
    throw new Error('O evento task.created não contém uma tarefa válida.');
  }

  const result = await reconcileCreatedEvent(task, loadCutoverRuntime());

  return result;
};

export default defineLogicFunction({
  universalIdentifier: 'cc1231f3-d464-4344-beff-56d9495f572c',
  name: 'task-cutover-created',
  description:
    'Aplica responsável, lista, etapa e datas padrão somente na criação nativa da tarefa.',
  timeoutSeconds: 10,
  databaseEventTriggerSettings: {
    eventName: 'task.created',
  },
  handler: handleTaskCreated,
});

export const TASK_OBJECT_UNIVERSAL_IDENTIFIER =
  STANDARD_OBJECT.task.universalIdentifier;
