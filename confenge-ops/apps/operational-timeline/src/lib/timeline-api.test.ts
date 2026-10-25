import { describe, expect, it, vi } from 'vitest';

import {
  fetchEveryRecord,
  groupTimelineLinksByTask,
  optionLabelsForField,
  parseTimelineDependency,
  recordsFromPage,
  taskTitlesFromRecords,
  type RestPage,
} from 'src/lib/timeline-api';

describe('Twenty REST pagination', () => {
  it('reads the real data + pageInfo envelope across cursors', async () => {
    const get = vi
      .fn()
      .mockResolvedValueOnce({
        data: { tasks: [{ id: 'task-1' }, { id: 'task-2' }] },
        pageInfo: { hasNextPage: true, endCursor: 'cursor-2' },
      } satisfies RestPage)
      .mockResolvedValueOnce({
        data: { tasks: [{ id: 'task-3' }] },
        pageInfo: { hasNextPage: false, endCursor: 'cursor-3' },
      } satisfies RestPage);

    const result = await fetchEveryRecord(
      { get } as never,
      '/rest/tasks',
    );

    expect(result.map((record) => record.id)).toEqual([
      'task-1',
      'task-2',
      'task-3',
    ]);
    expect(get).toHaveBeenNthCalledWith(1, '/rest/tasks', {
      query: { limit: 100, depth: 0, starting_after: undefined },
    });
    expect(get).toHaveBeenNthCalledWith(2, '/rest/tasks', {
      query: { limit: 100, depth: 0, starting_after: 'cursor-2' },
    });
  });

  it('fails closed if a broken server cycles through old cursors', async () => {
    const get = vi
      .fn()
      .mockResolvedValueOnce({
        data: [{ id: 'task-1' }],
        pageInfo: { hasNextPage: true, endCursor: 'cursor-a' },
      } satisfies RestPage)
      .mockResolvedValueOnce({
        data: [{ id: 'task-2' }],
        pageInfo: { hasNextPage: true, endCursor: 'cursor-b' },
      } satisfies RestPage)
      .mockResolvedValueOnce({
        data: [{ id: 'task-3' }],
        pageInfo: { hasNextPage: true, endCursor: 'cursor-a' },
      } satisfies RestPage);

    await expect(
      fetchEveryRecord({ get } as never, '/rest/tasks'),
    ).rejects.toThrow('paginação');
    expect(get).toHaveBeenCalledTimes(3);
  });

  it('deduplica registros repetidos entre páginas por id', async () => {
    const get = vi
      .fn()
      .mockResolvedValueOnce({
        data: [{ id: 'task-1', version: 1 }],
        pageInfo: { hasNextPage: true, endCursor: 'cursor-a' },
      } satisfies RestPage)
      .mockResolvedValueOnce({
        data: [
          { id: 'task-1', version: 2 },
          { id: 'task-2', version: 1 },
        ],
        pageInfo: { hasNextPage: false, endCursor: 'cursor-b' },
      } satisfies RestPage);

    const result = await fetchEveryRecord({ get } as never, '/rest/tasks');

    expect(result).toEqual([
      { id: 'task-1', version: 1 },
      { id: 'task-2', version: 1 },
    ]);
  });

  it('accepts array and metadata-style items envelopes defensively', () => {
    expect(recordsFromPage([{ id: 'a' }])).toEqual([{ id: 'a' }]);
    expect(recordsFromPage({ items: [{ id: 'b' }] })).toEqual([{ id: 'b' }]);
  });

  it('fails closed when a data envelope contains no unique collection', () => {
    expect(() => recordsFromPage({ data: {} })).toThrow('formato inesperado');
    expect(() =>
      recordsFromPage({ data: { tasks: [], people: [] } }),
    ).toThrow('formato inesperado');
  });
});

describe('UI metadata derived from authorized reads', () => {
  it('maps current select values to labels without changing stored values', () => {
    expect(
      optionLabelsForField(
        [
          {
            id: 'status-field',
            name: 'clickUpStatus',
            type: 'SELECT',
            options: [
              { value: 'S_INTERNAL', label: 'Em andamento' },
              { value: 'DONE_INTERNAL', label: 'Concluído' },
            ],
          },
        ],
        'clickUpStatus',
      ),
    ).toEqual({ S_INTERNAL: 'Em andamento', DONE_INTERNAL: 'Concluído' });
  });

  it('keeps titles for relation targets outside the filtered task rows', () => {
    expect(
      taskTitlesFromRecords([
        { id: 'visible', title: 'Marco visível' },
        { id: 'outside', title: 'Tarefa relacionada externa' },
      ]),
    ).toEqual({
      visible: 'Marco visível',
      outside: 'Tarefa relacionada externa',
    });
  });
});

describe('dependency direction', () => {
  it('does not invent a causal type when relationType is missing', () => {
    expect(() =>
      parseTimelineDependency({
        id: 'broken-dependency',
        fromTaskId: 'task-a',
        toTaskId: 'task-b',
      }),
    ).toThrow('relationType');
  });

  it('attaches A depends on B to A without marking B as dependent', () => {
    const dependency = {
      id: 'dep-1',
      fromTaskId: 'task-a',
      toTaskId: 'task-b',
      relationType: 'DEPENDS_ON',
    };
    const grouped = groupTimelineLinksByTask([dependency]);

    expect(grouped.get('task-a')?.dependsOn).toEqual([dependency]);
    expect(grouped.get('task-b')?.dependsOn).toBeUndefined();
  });

  it('shows RELATED on both tasks without treating it as dependency', () => {
    const related = {
      id: 'related-1',
      fromTaskId: 'task-a',
      toTaskId: 'task-b',
      relationType: 'RELATED',
    };
    const grouped = groupTimelineLinksByTask([related]);

    expect(grouped.get('task-a')).toEqual({ dependsOn: [], related: [related] });
    expect(grouped.get('task-b')).toEqual({ dependsOn: [], related: [related] });
  });
});
