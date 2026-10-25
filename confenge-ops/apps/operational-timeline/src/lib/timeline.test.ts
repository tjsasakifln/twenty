import { describe, expect, it } from 'vitest';

import {
  applyViewFilters,
  buildTimelineRange,
  parseTimelineDate,
  positionForDay,
  toTimelineTask,
} from 'src/lib/timeline';

describe('parseTimelineDate', () => {
  it('preserva data civil sem converter por UTC', () => {
    const parsed = parseTimelineDate('2026-10-25');

    expect(parsed).toMatchObject({
      raw: '2026-10-25',
      kind: 'DATE',
    });
    expect(parsed?.label).toContain('25');
  });

  it('projeta horário no dia civil de Brasília', () => {
    const parsed = parseTimelineDate('2026-10-25T01:00:00.000Z');
    const previousDay = parseTimelineDate('2026-10-24');

    expect(parsed?.kind).toBe('DATE_TIME');
    expect(parsed?.day).toBe(previousDay?.day);
    expect(parsed?.label).toMatch(/24.*22:00/);
  });

  it('rejeita datas civis impossíveis', () => {
    expect(parseTimelineDate('2026-02-30')).toBeNull();
  });
});

describe('toTimelineTask', () => {
  it('não inventa duração quando existe apenas prazo', () => {
    const task = toTimelineTask({
      id: 'task-1',
      title: 'Entrega',
      clickUpDueOn: '2026-12-01',
    });

    expect(task?.kind).toBe('MILESTONE');
    expect(task?.start).toBeNull();
    expect(task?.end?.raw).toBe('2026-12-01');
  });

  it('prioriza o prazo com hora quando disponível', () => {
    const task = toTimelineTask({
      id: 'task-2',
      title: 'Inspeção',
      dueAt: '2026-12-01T15:00:00.000Z',
      clickUpDueOn: '2026-12-01',
    });

    expect(task?.end?.kind).toBe('DATE_TIME');
  });
});

describe('applyViewFilters', () => {
  const fields = [
    { id: 'status-field', name: 'clickUpStatus', type: 'SELECT' },
    { id: 'list-field', name: 'clickUpList', type: 'RELATION' },
  ];

  it('aplica filtro select cuja configuração veio como JSON serializado', () => {
    const result = applyViewFilters(
      [
        { id: '1', clickUpStatus: 'marco' },
        { id: '2', clickUpStatus: 'execução' },
      ],
      fields,
      [
        {
          id: 'filter-1',
          fieldMetadataId: 'status-field',
          operand: 'IS',
          value: '["marco"]',
        },
      ],
    );

    expect(result.map((record) => record.id)).toEqual(['1']);
  });

  it('aceita relação normalizada pelo campo de id', () => {
    const result = applyViewFilters(
      [
        { id: '1', clickUpListId: 'list-contract' },
        { id: '2', clickUpListId: 'list-other' },
      ],
      fields,
      [
        {
          id: 'filter-2',
          fieldMetadataId: 'list-field',
          operand: 'IS',
          value: ['list-contract'],
        },
      ],
    );

    expect(result.map((record) => record.id)).toEqual(['1']);
  });

  it('aceita o formato real de filtro de relação do Twenty', () => {
    const result = applyViewFilters(
      [
        { id: '1', clickUpListId: 'list-contract' },
        { id: '2', clickUpListId: 'list-other' },
      ],
      fields,
      [
        {
          id: 'filter-real-relation',
          fieldMetadataId: 'list-field',
          operand: 'IS',
          value: {
            isCurrentRecordSelected: false,
            selectedRecordIds: ['list-contract'],
          },
        },
      ],
    );

    expect(result.map((record) => record.id)).toEqual(['1']);
  });

  it('não confunde booleanos internos de relações com IDs selecionados', () => {
    const result = applyViewFilters(
      [
        {
          id: 'wrong',
          clickUpList: { id: 'other', isActive: false },
        },
        {
          id: 'right',
          clickUpList: { id: 'wanted', isActive: false },
        },
      ],
      fields,
      [
        {
          id: 'filter-relation-boolean-regression',
          fieldMetadataId: 'list-field',
          operand: 'IS',
          value: {
            isCurrentRecordSelected: false,
            selectedRecordIds: ['wanted'],
          },
        },
      ],
    );

    expect(result.map((record) => record.id)).toEqual(['right']);
  });

  it('falha fechado em IS_NOT relacional com JSON malformado', () => {
    expect(() =>
      applyViewFilters(
        [{ id: '1', clickUpListId: 'list-contract' }],
        fields,
        [
          {
            id: 'filter-malformed-relation',
            fieldMetadataId: 'list-field',
            operand: 'IS_NOT',
            value: '{"selectedRecordIds":',
          },
        ],
      ),
    ).toThrow('JSON inválido');
  });

  it('respeita grupos OR sem depender do nome da visão', () => {
    const result = applyViewFilters(
      [
        { id: '1', clickUpStatus: 'marco', clickUpListId: 'other' },
        { id: '2', clickUpStatus: 'other', clickUpListId: 'list-contract' },
        { id: '3', clickUpStatus: 'other', clickUpListId: 'other' },
      ],
      fields,
      [
        {
          id: 'filter-3',
          fieldMetadataId: 'status-field',
          operand: 'IS',
          value: 'marco',
          viewFilterGroupId: 'group-1',
        },
        {
          id: 'filter-4',
          fieldMetadataId: 'list-field',
          operand: 'IS',
          value: 'list-contract',
          viewFilterGroupId: 'group-1',
        },
      ],
      [{ id: 'group-1', logicalOperator: 'OR' }],
    );

    expect(result.map((record) => record.id)).toEqual(['1', '2']);
  });

  it('falha fechado quando filtro aponta para grupo inexistente', () => {
    expect(() =>
      applyViewFilters(
        [{ id: '1', clickUpStatus: 'marco' }],
        fields,
        [
          {
            id: 'orphan-filter',
            fieldMetadataId: 'status-field',
            operand: 'IS',
            value: 'marco',
            viewFilterGroupId: 'missing-group',
          },
        ],
        [],
      ),
    ).toThrow('grupo inexistente');
  });

  it('falha fechado quando grupos de filtro formam ciclo', () => {
    expect(() =>
      applyViewFilters(
        [{ id: '1', clickUpStatus: 'marco' }],
        fields,
        [],
        [
          {
            id: 'group-a',
            parentViewFilterGroupId: 'group-b',
            logicalOperator: 'AND',
          },
          {
            id: 'group-b',
            parentViewFilterGroupId: 'group-a',
            logicalOperator: 'AND',
          },
        ],
      ),
    ).toThrow('ciclo');
  });
});

describe('timeline geometry', () => {
  it('posiciona início, meio e fim do domínio', () => {
    const first = toTimelineTask({ id: '1', title: 'A', clickUpDueOn: '2026-01-01' });
    const last = toTimelineTask({ id: '2', title: 'B', clickUpDueOn: '2026-01-11' });
    const range = buildTimelineRange([first!, last!]);

    expect(range).not.toBeNull();
    expect(positionForDay(first!.end!.day, range!)).toBe(0);
    expect(positionForDay(last!.end!.day, range!)).toBe(100);
    expect(positionForDay(first!.end!.day + 5, range!)).toBe(50);
  });
});
