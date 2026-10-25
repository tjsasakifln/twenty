import { afterEach, describe, expect, it } from 'vitest';

import {
  CUTOVER_VARIABLES,
  cutoverApiBaseUrl,
  loadCutoverRuntime,
  planCreatedTask,
  planUpdatedTask,
  readTask,
  reconcileCreatedEvent,
  reconcileUpdatedEvent,
  type CutoverRuntime,
  type TaskSnapshot,
} from 'src/logic-functions/task-cutover';
import taskCutoverCreated, {
  handleTaskCreated,
} from 'src/logic-functions/task-cutover-created.logic-function';
import taskCutoverUpdated, {
  handleTaskUpdated,
  WATCHED_TASK_FIELDS,
} from 'src/logic-functions/task-cutover-updated.logic-function';

const runtime: CutoverRuntime = {
  memberId: 'member-1',
  defaultListId: 'list-inbox',
  stageToStatus: {
    organize: 'TODO',
    planned: 'TODO',
    executing: 'IN_PROGRESS',
    reviewing: 'IN_PROGRESS',
    concluded: 'DONE',
  },
  statusToStage: {
    TODO: 'organize',
    IN_PROGRESS: 'executing',
    DONE: 'concluded',
  },
};

const task = (overrides: Partial<TaskSnapshot> = {}): TaskSnapshot => ({
  id: 'task-1',
  updatedAt: '2026-10-25T18:00:00.000Z',
  assigneeId: 'member-1',
  clickUpListId: 'list-a',
  status: 'TODO',
  clickUpStatus: 'organize',
  cutoverStageBeforeDone: null,
  clickUpStartOn: null,
  clickUpStartAt: null,
  clickUpDueOn: null,
  dueAt: null,
  ...overrides,
});

describe('task cutover trigger manifest contract', () => {
  it('subscribes the create handler to the singular Twenty object event', () => {
    expect(taskCutoverCreated.config.databaseEventTriggerSettings).toEqual({
      eventName: 'task.created',
    });
  });

  it('subscribes the update handler to the singular event and every watched field', () => {
    expect(taskCutoverUpdated.config.databaseEventTriggerSettings).toEqual({
      eventName: 'task.updated',
      updatedFields: [...WATCHED_TASK_FIELDS],
    });
  });
});

describe('task cutover event planning', () => {
  it('applies defaults once when a task is created outside a list view', () => {
    expect(
      planCreatedTask(
        task({
          assigneeId: null,
          clickUpListId: null,
          clickUpStatus: null,
        }),
        runtime,
      ),
    ).toEqual({
      kind: 'patch',
      patch: {
        assigneeId: 'member-1',
        clickUpListId: 'list-inbox',
        clickUpStatus: 'organize',
      },
    });
  });

  it('preserves a relation supplied by creation inside a filtered view', () => {
    const plan = planCreatedTask(
      task({ clickUpListId: 'list-from-view', assigneeId: null }),
      runtime,
    );

    expect(plan.patch).not.toHaveProperty('clickUpListId');
    expect(plan.patch).toHaveProperty('assigneeId', 'member-1');
  });

  it('re-reads and converges creation defaults after an unrelated title race', async () => {
    const appliedPlans: Array<{ task: TaskSnapshot; patch: object }> = [];
    const initial = task({
      updatedAt: '2026-10-25T18:00:00.000Z',
      assigneeId: null,
      clickUpListId: null,
      clickUpStatus: null,
    });
    const current = {
      ...initial,
      updatedAt: '2026-10-25T18:00:01.000Z',
      title: 'Título editado pelo usuário',
    };

    const result = await reconcileCreatedEvent(
      initial,
      runtime,
      async () => current,
      async (currentTask, plan) => {
        appliedPlans.push({ task: currentTask, patch: plan.patch });

        return appliedPlans.length === 1
          ? { kind: 'stale' as const, applied: false }
          : { kind: 'patched' as const, applied: true };
      },
    );

    expect(result).toEqual({ kind: 'patched', applied: true });
    expect(appliedPlans).toHaveLength(2);
    expect(appliedPlans[1].task.updatedAt).toBe(
      '2026-10-25T18:00:01.000Z',
    );
    expect(appliedPlans[1].patch).toEqual({
      assigneeId: 'member-1',
      clickUpListId: 'list-inbox',
      clickUpStatus: 'organize',
    });
    expect(appliedPlans[1].patch).not.toHaveProperty('title');
  });

  it('maps the completion button to the canonical concluded stage', () => {
    expect(
      planUpdatedTask(
        task({ status: 'DONE', clickUpStatus: 'executing' }),
        ['status'],
        runtime,
      ),
    ).toEqual({
      kind: 'patch',
      patch: {
        clickUpStatus: 'concluded',
        cutoverStageBeforeDone: 'executing',
      },
    });
  });

  it('restores the exact detailed stage when a completed task is reopened', () => {
    expect(
      planUpdatedTask(
        task({
          status: 'TODO',
          clickUpStatus: 'concluded',
          cutoverStageBeforeDone: 'reviewing',
        }),
        ['status'],
        runtime,
      ),
    ).toEqual({
      kind: 'patch',
      patch: {
        clickUpStatus: 'reviewing',
        status: 'IN_PROGRESS',
        cutoverStageBeforeDone: null,
      },
    });
  });

  it('preserves the detailed stage atomically when Etapa completes the task', () => {
    const before = task({
      status: 'IN_PROGRESS',
      clickUpStatus: 'reviewing',
    });

    expect(
      planUpdatedTask(
        task({ status: 'IN_PROGRESS', clickUpStatus: 'concluded' }),
        ['clickUpStatus'],
        runtime,
        before,
      ),
    ).toEqual({
      kind: 'patch',
      patch: {
        status: 'DONE',
        cutoverStageBeforeDone: 'reviewing',
      },
    });

    expect(
      planUpdatedTask(
        task({
          status: 'TODO',
          clickUpStatus: 'concluded',
          cutoverStageBeforeDone: 'reviewing',
        }),
        ['status'],
        runtime,
      ),
    ).toEqual({
      kind: 'patch',
      patch: {
        clickUpStatus: 'reviewing',
        status: 'IN_PROGRESS',
        cutoverStageBeforeDone: null,
      },
    });
  });

  it('maintains stage memory when status and Etapa change coherently together', () => {
    expect(
      planUpdatedTask(
        task({ status: 'DONE', clickUpStatus: 'concluded' }),
        ['status', 'clickUpStatus'],
        runtime,
        task({ status: 'IN_PROGRESS', clickUpStatus: 'reviewing' }),
      ),
    ).toEqual({
      kind: 'patch',
      patch: { cutoverStageBeforeDone: 'reviewing' },
    });

    expect(
      planUpdatedTask(
        task({
          status: 'IN_PROGRESS',
          clickUpStatus: 'reviewing',
          cutoverStageBeforeDone: 'reviewing',
        }),
        ['status', 'clickUpStatus'],
        runtime,
        task({ status: 'DONE', clickUpStatus: 'concluded' }),
      ),
    ).toEqual({
      kind: 'patch',
      patch: { cutoverStageBeforeDone: null },
    });
  });

  it('raises an observable error after three stale update attempts', async () => {
    const initial = task({
      status: 'DONE',
      clickUpStatus: 'executing',
      updatedAt: '2026-10-25T18:00:00.000Z',
    });
    let readCount = 0;
    let applyCount = 0;

    await expect(
      reconcileUpdatedEvent(
        initial,
        ['status'],
        runtime,
        async () => {
          readCount += 1;
          return task({
            status: 'DONE',
            clickUpStatus: 'executing',
            updatedAt: `2026-10-25T18:00:0${readCount}.000Z`,
          });
        },
        async () => {
          applyCount += 1;
          return { kind: 'stale' as const, applied: false };
        },
      ),
    ).rejects.toThrow('stale após 3 tentativas');
    expect(applyCount).toBe(3);
  });

  it('does not retry a stale status event after the paired stage changed', async () => {
    const initial = task({
      status: 'DONE',
      clickUpStatus: 'executing',
      updatedAt: '2026-10-25T18:00:00.000Z',
    });
    const concurrent = task({
      status: 'DONE',
      clickUpStatus: 'reviewing',
      updatedAt: '2026-10-25T18:00:01.000Z',
    });
    let applyCount = 0;

    const result = await reconcileUpdatedEvent(
      initial,
      ['status'],
      runtime,
      async () => concurrent,
      async () => {
        applyCount += 1;
        return { kind: 'stale' as const, applied: false };
      },
    );

    expect(result).toEqual({ kind: 'superseded', applied: false });
    expect(applyCount).toBe(1);
  });

  it('does not retry a stale timed-date event after the date-only pair changed', async () => {
    const initial = task({
      clickUpDueOn: '2026-10-30',
      dueAt: '2026-10-30T14:00:00.000Z',
      updatedAt: '2026-10-25T18:00:00.000Z',
    });
    const concurrent = task({
      clickUpDueOn: '2026-10-31',
      dueAt: '2026-10-30T14:00:00.000Z',
      updatedAt: '2026-10-25T18:00:01.000Z',
    });
    let applyCount = 0;

    const result = await reconcileUpdatedEvent(
      initial,
      ['dueAt'],
      runtime,
      async () => concurrent,
      async () => {
        applyCount += 1;
        return { kind: 'stale' as const, applied: false };
      },
    );

    expect(result).toEqual({ kind: 'superseded', applied: false });
    expect(applyCount).toBe(1);
  });

  it('maps a detailed stage edit back to the native status', () => {
    expect(
      planUpdatedTask(
        task({ status: 'TODO', clickUpStatus: 'reviewing' }),
        ['clickUpStatus'],
        runtime,
      ),
    ).toEqual({ kind: 'patch', patch: { status: 'IN_PROGRESS' } });
  });

  it('does not create an event loop after the counterpart is coherent', () => {
    expect(
      planUpdatedTask(
        task({ status: 'DONE', clickUpStatus: 'concluded' }),
        ['clickUpStatus'],
        runtime,
      ),
    ).toEqual({ kind: 'noop', patch: {} });
  });

  it('preserves a detailed stage on the status echo emitted by a stage edit', () => {
    expect(
      planUpdatedTask(
        task({ status: 'IN_PROGRESS', clickUpStatus: 'reviewing' }),
        ['status'],
        runtime,
        task({ status: 'TODO', clickUpStatus: 'reviewing' }),
      ),
    ).toEqual({ kind: 'noop', patch: {} });

    expect(
      planUpdatedTask(
        task({
          status: 'DONE',
          clickUpStatus: 'concluded',
          cutoverStageBeforeDone: 'reviewing',
        }),
        ['status'],
        runtime,
        task({
          status: 'IN_PROGRESS',
          clickUpStatus: 'concluded',
          cutoverStageBeforeDone: 'reviewing',
        }),
      ),
    ).toEqual({ kind: 'noop', patch: {} });
  });

  it('clears stale completion memory on a coherent non-DONE status echo', () => {
    expect(
      planUpdatedTask(
        task({
          status: 'IN_PROGRESS',
          clickUpStatus: 'reviewing',
          cutoverStageBeforeDone: 'reviewing',
        }),
        ['status'],
        runtime,
      ),
    ).toEqual({
      kind: 'patch',
      patch: { cutoverStageBeforeDone: null },
    });
  });

  it('accepts the coherent combined echo emitted by an exact reopen patch', () => {
    expect(
      planUpdatedTask(
        task({
          status: 'IN_PROGRESS',
          clickUpStatus: 'reviewing',
          cutoverStageBeforeDone: null,
        }),
        ['status', 'clickUpStatus'],
        runtime,
        task({
          status: 'TODO',
          clickUpStatus: 'concluded',
          cutoverStageBeforeDone: 'reviewing',
        }),
      ),
    ).toEqual({ kind: 'noop', patch: {} });
  });

  it('reports simultaneous incompatible status edits without overwriting either', () => {
    const plan = planUpdatedTask(
      task({ status: 'DONE', clickUpStatus: 'reviewing' }),
      ['status', 'clickUpStatus'],
      runtime,
    );

    expect(plan.kind).toBe('conflict');
    expect(plan.patch).toEqual({});
  });

  it('keeps one date representation when a timed deadline is added', () => {
    expect(
      planUpdatedTask(
        task({
          clickUpDueOn: '2026-10-30',
          dueAt: '2026-10-30T14:00:00.000Z',
        }),
        ['dueAt'],
        runtime,
      ),
    ).toEqual({ kind: 'patch', patch: { clickUpDueOn: null } });
  });

  it('keeps one date representation when a date-only deadline is added', () => {
    expect(
      planUpdatedTask(
        task({
          clickUpDueOn: '2026-10-30',
          dueAt: '2026-10-30T14:00:00.000Z',
        }),
        ['clickUpDueOn'],
        runtime,
      ),
    ).toEqual({ kind: 'patch', patch: { dueAt: null } });
  });

  it('reports a conflicting two-field date edit without data loss', () => {
    const plan = planUpdatedTask(
      task({
        clickUpStartOn: '2026-10-30',
        clickUpStartAt: '2026-10-30T14:00:00.000Z',
      }),
      ['clickUpStartOn', 'clickUpStartAt'],
      runtime,
    );

    expect(plan.kind).toBe('conflict');
    expect(plan.patch).toEqual({});
  });
});

describe('task cutover runtime', () => {
  const originalEnvironment = { ...process.env };

  afterEach(() => {
    process.env = { ...originalEnvironment };
  });

  it('loads target-bound values without committing them in source', () => {
    process.env[CUTOVER_VARIABLES.memberId] = 'member-1';
    process.env[CUTOVER_VARIABLES.defaultListId] = 'list-inbox';
    process.env[CUTOVER_VARIABLES.stageStatusMap] = JSON.stringify(
      runtime.stageToStatus,
    );
    process.env[CUTOVER_VARIABLES.todoStage] = 'organize';
    process.env[CUTOVER_VARIABLES.inProgressStage] = 'executing';
    process.env[CUTOVER_VARIABLES.doneStage] = 'concluded';

    expect(loadCutoverRuntime()).toEqual(runtime);
  });

  it('pins API calls to the authorized internal Twenty endpoint', () => {
    process.env[CUTOVER_VARIABLES.apiBaseUrl] = 'http://server:3000/';

    expect(cutoverApiBaseUrl()).toBe('http://server:3000');
  });

  it.each([
    'https://gestao.confenge.com.br',
    'http://server:3001',
    'http://db:5432',
    'http://server:3000/graphql',
    'http://user:password@server:3000',
    'http://server:3000?redirect=http://db:5432',
  ])('rejects an API endpoint outside the strict allowlist: %s', (value) => {
    process.env[CUTOVER_VARIABLES.apiBaseUrl] = value;

    expect(() => cutoverApiBaseUrl()).toThrow(
      'deve apontar exatamente para o endpoint interno autorizado',
    );
  });

  it('pins CAS rereads to depth zero so limited roles do not expand relations', async () => {
    const calls: unknown[][] = [];
    const snapshot = task();
    const client = {
      get: async (...args: unknown[]) => {
        calls.push(args);
        return snapshot;
      },
    };

    await expect(readTask(snapshot.id, client as never)).resolves.toEqual(
      snapshot,
    );
    expect(calls).toEqual([
      [`/rest/tasks/${snapshot.id}`, { query: { depth: 0 } }],
    ]);
  });

  it('fails closed before reading variables or calling APIs when disabled', async () => {
    delete process.env.CUTOVER_ENABLED;
    delete process.env[CUTOVER_VARIABLES.memberId];

    await expect(
      handleTaskCreated({ properties: {} } as never),
    ).resolves.toEqual({ kind: 'disabled', applied: false });
    await expect(
      handleTaskUpdated({ properties: {} } as never),
    ).resolves.toEqual({ kind: 'disabled', applied: false });
  });

  it('treats every CUTOVER_ENABLED value except exact true as disabled', async () => {
    process.env.CUTOVER_ENABLED = 'TRUE';

    await expect(
      handleTaskCreated({ properties: {} } as never),
    ).resolves.toEqual({ kind: 'disabled', applied: false });
  });

  it('enters normal validation when CUTOVER_ENABLED is exact true', async () => {
    process.env.CUTOVER_ENABLED = 'true';
    delete process.env[CUTOVER_VARIABLES.memberId];

    await expect(
      handleTaskCreated({
        properties: { after: task() },
      } as never),
    ).rejects.toThrow('CUTOVER_STAGE_STATUS_MAP');
  });
});
