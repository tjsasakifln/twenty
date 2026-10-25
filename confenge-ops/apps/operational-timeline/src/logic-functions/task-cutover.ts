import { RestApiClient } from 'twenty-client-sdk/rest';

export const CUTOVER_VARIABLES = {
  apiBaseUrl: 'CUTOVER_API_BASE_URL',
  memberId: 'CUTOVER_MEMBER_ID',
  defaultListId: 'CUTOVER_DEFAULT_LIST_ID',
  stageStatusMap: 'CUTOVER_STAGE_STATUS_MAP',
  todoStage: 'CUTOVER_STAGE_TODO',
  inProgressStage: 'CUTOVER_STAGE_IN_PROGRESS',
  doneStage: 'CUTOVER_STAGE_DONE',
} as const;

const ALLOWED_CUTOVER_API_BASE_URL = 'http://server:3000';

export const isCutoverEnabled = () => process.env.CUTOVER_ENABLED === 'true';

export type NativeStatus = 'TODO' | 'IN_PROGRESS' | 'DONE';

export type TaskSnapshot = {
  id: string;
  updatedAt: string;
  assigneeId?: string | null;
  clickUpListId?: string | null;
  status?: NativeStatus | null;
  clickUpStatus?: string | null;
  cutoverStageBeforeDone?: string | null;
  clickUpStartOn?: string | null;
  clickUpStartAt?: string | null;
  clickUpDueOn?: string | null;
  dueAt?: string | null;
};

export type CutoverRuntime = {
  memberId: string;
  defaultListId: string;
  stageToStatus: Record<string, NativeStatus>;
  statusToStage: Record<NativeStatus, string>;
};

export type TaskPatch = Partial<
  Pick<
    TaskSnapshot,
    | 'assigneeId'
    | 'clickUpListId'
    | 'status'
    | 'clickUpStatus'
    | 'cutoverStageBeforeDone'
    | 'clickUpStartOn'
    | 'clickUpStartAt'
    | 'clickUpDueOn'
    | 'dueAt'
  >
>;

export type ReconciliationPlan =
  | { kind: 'noop'; patch: TaskPatch }
  | { kind: 'patch'; patch: TaskPatch }
  | { kind: 'conflict'; patch: TaskPatch; reason: string };

const NATIVE_STATUSES = new Set<NativeStatus>([
  'TODO',
  'IN_PROGRESS',
  'DONE',
]);

const requiredEnvironmentVariable = (name: string): string => {
  const value = process.env[name]?.trim();

  if (!value) {
    throw new Error(`A variável obrigatória ${name} não está configurada.`);
  }

  return value;
};

export const cutoverApiBaseUrl = (): string => {
  const value = requiredEnvironmentVariable(CUTOVER_VARIABLES.apiBaseUrl);

  let parsed: URL;

  try {
    parsed = new URL(value);
  } catch {
    throw new Error(
      `${CUTOVER_VARIABLES.apiBaseUrl} precisa conter uma URL interna válida.`,
    );
  }

  const normalized = `${parsed.protocol}//${parsed.host}${
    parsed.pathname === '/' ? '' : parsed.pathname
  }`;
  const hasUnexpectedParts =
    parsed.username !== '' ||
    parsed.password !== '' ||
    parsed.search !== '' ||
    parsed.hash !== '';

  if (hasUnexpectedParts || normalized !== ALLOWED_CUTOVER_API_BASE_URL) {
    throw new Error(
      `${CUTOVER_VARIABLES.apiBaseUrl} deve apontar exatamente para o endpoint interno autorizado.`,
    );
  }

  return ALLOWED_CUTOVER_API_BASE_URL;
};

const cutoverRestClient = () =>
  new RestApiClient({
    runAs: 'application',
    baseUrl: cutoverApiBaseUrl(),
  });

export const loadCutoverRuntime = (): CutoverRuntime => {
  const rawMap = requiredEnvironmentVariable(CUTOVER_VARIABLES.stageStatusMap);
  let parsed: unknown;

  try {
    parsed = JSON.parse(rawMap);
  } catch {
    throw new Error('CUTOVER_STAGE_STATUS_MAP precisa ser um objeto JSON válido.');
  }

  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('CUTOVER_STAGE_STATUS_MAP precisa ser um objeto JSON.');
  }

  const stageToStatus: Record<string, NativeStatus> = {};

  for (const [stage, status] of Object.entries(parsed)) {
    if (!stage || !NATIVE_STATUSES.has(status as NativeStatus)) {
      throw new Error('CUTOVER_STAGE_STATUS_MAP contém uma entrada inválida.');
    }

    stageToStatus[stage] = status as NativeStatus;
  }

  if (Object.keys(stageToStatus).length === 0) {
    throw new Error('CUTOVER_STAGE_STATUS_MAP não pode ser vazio.');
  }

  const statusToStage = {
    TODO: requiredEnvironmentVariable(CUTOVER_VARIABLES.todoStage),
    IN_PROGRESS: requiredEnvironmentVariable(
      CUTOVER_VARIABLES.inProgressStage,
    ),
    DONE: requiredEnvironmentVariable(CUTOVER_VARIABLES.doneStage),
  } satisfies Record<NativeStatus, string>;

  for (const [status, stage] of Object.entries(statusToStage)) {
    if (stageToStatus[stage] !== status) {
      throw new Error(
        `A etapa canônica de ${status} não corresponde ao mapa de etapas.`,
      );
    }
  }

  return {
    memberId: requiredEnvironmentVariable(CUTOVER_VARIABLES.memberId),
    defaultListId: requiredEnvironmentVariable(
      CUTOVER_VARIABLES.defaultListId,
    ),
    stageToStatus,
    statusToStage,
  };
};

const hasOwn = (value: object, key: string) =>
  Object.prototype.hasOwnProperty.call(value, key);

const withoutUnchangedValues = (
  task: TaskSnapshot,
  candidate: TaskPatch,
): TaskPatch =>
  Object.fromEntries(
    Object.entries(candidate).filter(
      ([field, value]) => task[field as keyof TaskSnapshot] !== value,
    ),
  );

export const planCreatedTask = (
  task: TaskSnapshot,
  runtime: CutoverRuntime,
): ReconciliationPlan => {
  const patch: TaskPatch = {};

  if (!task.assigneeId) {
    patch.assigneeId = runtime.memberId;
  }

  if (!task.clickUpListId) {
    patch.clickUpListId = runtime.defaultListId;
  }

  const status = task.status ?? 'TODO';
  const stage = task.clickUpStatus;

  if (!stage) {
    patch.clickUpStatus = runtime.statusToStage[status];
  } else {
    const expectedStatus = runtime.stageToStatus[stage];

    if (!expectedStatus) {
      return {
        kind: 'conflict',
        patch: {},
        reason: 'A nova tarefa contém uma etapa não mapeada.',
      };
    }

    if (expectedStatus !== status) {
      patch.status = expectedStatus;
    }
  }

  // A timestamp is more precise than the duplicated calendar date retained by
  // the importer.  New writes keep exactly one representation populated.
  if (task.clickUpStartAt && task.clickUpStartOn) {
    patch.clickUpStartOn = null;
  }

  if (task.dueAt && task.clickUpDueOn) {
    patch.clickUpDueOn = null;
  }

  const effectivePatch = withoutUnchangedValues(task, patch);

  return {
    kind: Object.keys(effectivePatch).length === 0 ? 'noop' : 'patch',
    patch: effectivePatch,
  };
};

const planExclusiveDatePair = (
  task: TaskSnapshot,
  updated: Set<string>,
  dateField: 'clickUpStartOn' | 'clickUpDueOn',
  dateTimeField: 'clickUpStartAt' | 'dueAt',
): ReconciliationPlan => {
  const dateChanged = updated.has(dateField);
  const dateTimeChanged = updated.has(dateTimeField);
  const date = task[dateField];
  const dateTime = task[dateTimeField];

  if (dateChanged && dateTimeChanged) {
    if (date && dateTime) {
      return {
        kind: 'conflict',
        patch: {},
        reason: `${dateField} e ${dateTimeField} foram preenchidos juntos.`,
      };
    }

    return { kind: 'noop', patch: {} };
  }

  if (dateChanged && date && dateTime) {
    return { kind: 'patch', patch: { [dateTimeField]: null } };
  }

  if (dateTimeChanged && dateTime && date) {
    return { kind: 'patch', patch: { [dateField]: null } };
  }

  return { kind: 'noop', patch: {} };
};

export const planUpdatedTask = (
  task: TaskSnapshot,
  updatedFields: string[],
  runtime: CutoverRuntime,
  beforeTask?: TaskSnapshot,
): ReconciliationPlan => {
  const updated = new Set(updatedFields);
  const patch: TaskPatch = {};
  const statusChanged = updated.has('status');
  const stageChanged = updated.has('clickUpStatus');

  if (statusChanged || stageChanged) {
    const status = task.status ?? 'TODO';
    const stage = task.clickUpStatus;

    if (statusChanged && stageChanged) {
      if (!stage || runtime.stageToStatus[stage] !== status) {
        return {
          kind: 'conflict',
          patch: {},
          reason: 'Situação geral e Etapa mudaram juntas para valores incompatíveis.',
        };
      }
      const beforeStage = beforeTask?.clickUpStatus;

      if (
        status === 'DONE' &&
        beforeStage &&
        runtime.stageToStatus[beforeStage] !== 'DONE'
      ) {
        patch.cutoverStageBeforeDone = beforeStage;
      } else if (status !== 'DONE' && task.cutoverStageBeforeDone) {
        patch.cutoverStageBeforeDone = null;
      }
    } else if (statusChanged) {
      if (stage && runtime.stageToStatus[stage] === status) {
        // A stage-first edit emits a second event for the status patched by
        // this function.  The already-coherent detailed stage is not a new
        // native-button intent and must survive that echo event.
        if (status !== 'DONE' && task.cutoverStageBeforeDone) {
          patch.cutoverStageBeforeDone = null;
        }
      } else if (status === 'DONE') {
        patch.clickUpStatus = runtime.statusToStage.DONE;
        if (stage && runtime.stageToStatus[stage] !== 'DONE') {
          patch.cutoverStageBeforeDone = stage;
        }
      } else if (
        stage &&
        runtime.stageToStatus[stage] === 'DONE' &&
        task.cutoverStageBeforeDone &&
        runtime.stageToStatus[task.cutoverStageBeforeDone]
      ) {
        // "Reopen" returns to the exact detailed stage from before completion.
        // Its coarse native status follows that restored stage, so the two
        // fields remain coherent even when the button emits TODO.
        patch.clickUpStatus = task.cutoverStageBeforeDone;
        patch.status = runtime.stageToStatus[task.cutoverStageBeforeDone];
        patch.cutoverStageBeforeDone = null;
      } else {
        patch.clickUpStatus = runtime.statusToStage[status];
      }
    } else if (stageChanged) {
      if (!stage || !runtime.stageToStatus[stage]) {
        return {
          kind: 'conflict',
          patch: {},
          reason: 'A tarefa contém uma Etapa não mapeada.',
        };
      }

      patch.status = runtime.stageToStatus[stage];
      const beforeStage = beforeTask?.clickUpStatus;

      if (
        runtime.stageToStatus[stage] === 'DONE' &&
        beforeStage &&
        runtime.stageToStatus[beforeStage] !== 'DONE'
      ) {
        patch.cutoverStageBeforeDone = beforeStage;
      } else if (
        runtime.stageToStatus[stage] !== 'DONE' &&
        task.cutoverStageBeforeDone
      ) {
        patch.cutoverStageBeforeDone = null;
      }
    }
  }

  for (const plan of [
    planExclusiveDatePair(
      task,
      updated,
      'clickUpStartOn',
      'clickUpStartAt',
    ),
    planExclusiveDatePair(task, updated, 'clickUpDueOn', 'dueAt'),
  ]) {
    if (plan.kind === 'conflict') {
      return plan;
    }

    Object.assign(patch, plan.patch);
  }

  const effectivePatch = withoutUnchangedValues(task, patch);

  return {
    kind: Object.keys(effectivePatch).length === 0 ? 'noop' : 'patch',
    patch: effectivePatch,
  };
};

type GraphQlResponse = {
  data?: { updateTasks?: Array<{ id: string; updatedAt: string }> };
  errors?: Array<{ message?: string }>;
};

const UPDATE_TASKS_WITH_CAS = `
  mutation CutoverUpdateTask($filter: TaskFilterInput!, $data: TaskUpdateInput!) {
    updateTasks(filter: $filter, data: $data) { id updatedAt }
  }
`;

export const updateTaskWithCompareAndSwap = async (
  task: TaskSnapshot,
  patch: TaskPatch,
): Promise<{ applied: boolean; conflict: boolean }> => {
  if (Object.keys(patch).length === 0) {
    return { applied: false, conflict: false };
  }

  const client = cutoverRestClient();
  const response = await client.post<GraphQlResponse>('/graphql', {
    query: UPDATE_TASKS_WITH_CAS,
    variables: {
      filter: {
        id: { eq: task.id },
        updatedAt: { eq: task.updatedAt },
      },
      data: patch,
    },
  });

  if (response.errors?.length) {
    throw new Error(
      response.errors.map((error) => error.message ?? 'GraphQL error').join('; '),
    );
  }

  const updated = response.data?.updateTasks ?? [];

  if (updated.length > 1) {
    throw new Error('A atualização CAS alterou mais de uma tarefa.');
  }

  return { applied: updated.length === 1, conflict: updated.length === 0 };
};

export const reconcileEvent = async (
  task: TaskSnapshot,
  plan: ReconciliationPlan,
) => {
  if (plan.kind !== 'patch') {
    return {
      kind: plan.kind,
      applied: false,
      reason: plan.kind === 'conflict' ? plan.reason : undefined,
    };
  }

  const result = await updateTaskWithCompareAndSwap(task, plan.patch);

  return {
    kind: result.conflict ? ('stale' as const) : ('patched' as const),
    applied: result.applied,
  };
};

const taskFromResponse = (response: unknown): TaskSnapshot => {
  let value = response;

  if (value && typeof value === 'object' && 'data' in value) {
    const data = (value as { data?: unknown }).data;

    if (data && typeof data === 'object' && 'id' in data) {
      value = data;
    } else if (data && typeof data === 'object') {
      const nested = Object.values(data).find(
        (item) => item && typeof item === 'object' && 'id' in item,
      );

      if (nested) {
        value = nested;
      }
    }
  }

  if (!isTaskSnapshot(value)) {
    throw new Error('A releitura CAS não retornou uma tarefa válida.');
  }

  return value;
};

export const readTask = async (
  taskId: string,
  client: Pick<RestApiClient, 'get'> = cutoverRestClient(),
): Promise<TaskSnapshot> =>
  taskFromResponse(
    await client.get(`/rest/tasks/${taskId}`, { query: { depth: 0 } }),
  );

export const reconcileCreatedEvent = async (
  initialTask: TaskSnapshot,
  runtime: CutoverRuntime,
  readCurrent: (taskId: string) => Promise<TaskSnapshot> = readTask,
  applyPlan: typeof reconcileEvent = reconcileEvent,
) => {
  let task = initialTask;

  for (let attempt = 0; attempt < 3; attempt += 1) {
    const result = await applyPlan(task, planCreatedTask(task, runtime));

    if (result.kind !== 'stale') {
      return result;
    }

    task = await readCurrent(task.id);
  }

  throw new Error('CAS da criação permaneceu stale após 3 tentativas.');
};

export const reconcileUpdatedEvent = async (
  initialTask: TaskSnapshot,
  updatedFields: string[],
  runtime: CutoverRuntime,
  readCurrent: (taskId: string) => Promise<TaskSnapshot> = readTask,
  applyPlan: typeof reconcileEvent = reconcileEvent,
  beforeTask?: TaskSnapshot,
) => {
  let task = initialTask;
  const invariantFields = new Set<keyof TaskSnapshot>();

  if (
    updatedFields.includes('status') ||
    updatedFields.includes('clickUpStatus')
  ) {
    invariantFields.add('status');
    invariantFields.add('clickUpStatus');
    invariantFields.add('cutoverStageBeforeDone');
  }

  if (
    updatedFields.includes('clickUpStartOn') ||
    updatedFields.includes('clickUpStartAt')
  ) {
    invariantFields.add('clickUpStartOn');
    invariantFields.add('clickUpStartAt');
  }

  if (
    updatedFields.includes('clickUpDueOn') ||
    updatedFields.includes('dueAt')
  ) {
    invariantFields.add('clickUpDueOn');
    invariantFields.add('dueAt');
  }

  for (let attempt = 0; attempt < 3; attempt += 1) {
    const result = await applyPlan(
      task,
      planUpdatedTask(task, updatedFields, runtime, beforeTask),
    );

    if (result.kind !== 'stale') {
      return result;
    }

    const current = await readCurrent(task.id);
    const invariantChanged = [...invariantFields].some(
      (field) => current[field] !== task[field],
    );

    if (invariantChanged) {
      // A newer database event owns the newer intent; never overwrite it with
      // a stale handler retry.
      return { kind: 'superseded' as const, applied: false };
    }

    task = current;
  }

  throw new Error('CAS da atualização permaneceu stale após 3 tentativas.');
};

export const isTaskSnapshot = (value: unknown): value is TaskSnapshot => {
  if (!value || typeof value !== 'object') {
    return false;
  }

  const record = value as Record<string, unknown>;

  return (
    typeof record.id === 'string' &&
    typeof record.updatedAt === 'string' &&
    (!hasOwn(record, 'status') ||
      record.status === null ||
      NATIVE_STATUSES.has(record.status as NativeStatus))
  );
};
