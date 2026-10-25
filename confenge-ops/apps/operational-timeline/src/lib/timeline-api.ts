import { MetadataApiClient } from 'twenty-client-sdk/metadata';
import { RestApiClient } from 'twenty-client-sdk/rest';
import { getApplicationVariable } from 'twenty-sdk/front-component';

import {
  applyViewFilters,
  type FieldMetadataLike,
  type UnknownRecord,
  type ViewFilterGroupLike,
  type ViewFilterLike,
} from 'src/lib/timeline';

export type RestPage = {
  data?: UnknownRecord[] | Record<string, UnknownRecord[]>;
  items?: UnknownRecord[];
  pageInfo?: {
    hasNextPage?: boolean;
    endCursor?: string | null;
  };
};

export type TimelineDependency = {
  id: string;
  fromTaskId: string;
  toTaskId: string;
  relationType: string;
};

export type TimelinePayload = {
  tasks: UnknownRecord[];
  dependencies: TimelineDependency[];
  viewName: string;
};

export type TimelineTaskLinks = {
  dependsOn: TimelineDependency[];
  related: TimelineDependency[];
};

export const groupTimelineLinksByTask = (dependencies: TimelineDependency[]) => {
  const map = new Map<string, TimelineTaskLinks>();
  const entry = (taskId: string) => {
    const existing = map.get(taskId);

    if (existing) {
      return existing;
    }

    const created = { dependsOn: [], related: [] };
    map.set(taskId, created);
    return created;
  };

  for (const dependency of dependencies) {
    const relationType = dependency.relationType.toUpperCase();

    if (relationType === 'DEPENDS_ON') {
      // Importer contract: fromTask is dependent; toTask is required.
      entry(dependency.fromTaskId).dependsOn.push(dependency);
    } else if (relationType === 'RELATED') {
      // RELATED has no causal direction, so make it visible on both tasks.
      entry(dependency.fromTaskId).related.push(dependency);
      entry(dependency.toTaskId).related.push(dependency);
    }
  }

  return map;
};

export const recordsFromPage = (page: RestPage | UnknownRecord[]) => {
  if (Array.isArray(page)) {
    return page;
  }

  if (Array.isArray(page.data)) {
    return page.data;
  }

  if (page.data && typeof page.data === 'object') {
    const collections = Object.values(page.data).filter(Array.isArray);

    if (collections.length !== 1) {
      throw new Error('A resposta REST do Twenty possui formato inesperado.');
    }

    return collections[0];
  }

  return page.items ?? [];
};

export const fetchEveryRecord = async (
  client: RestApiClient,
  path: string,
): Promise<UnknownRecord[]> => {
  const records: UnknownRecord[] = [];
  const seenRecordIds = new Set<string>();
  const seenCursors = new Set<string>();
  let cursor: string | undefined;

  for (let pageNumber = 0; pageNumber < 100; pageNumber += 1) {
    const page = await client.get<RestPage | UnknownRecord[]>(path, {
      query: {
        limit: 100,
        // Depth zero keeps scalar foreign keys (for example clickUpListId,
        // fromTaskId and toTaskId) without expanding relations outside the
        // app's least-privilege role.
        depth: 0,
        starting_after: cursor,
      },
    });
    for (const record of recordsFromPage(page)) {
      const recordId = typeof record.id === 'string' ? record.id : null;

      if (recordId && seenRecordIds.has(recordId)) {
        continue;
      }

      if (recordId) {
        seenRecordIds.add(recordId);
      }

      records.push(record);
    }

    if (Array.isArray(page) || !page.pageInfo?.hasNextPage) {
      return records;
    }

    const nextCursor = page.pageInfo.endCursor ?? undefined;

    if (!nextCursor || seenCursors.has(nextCursor)) {
      throw new Error('A paginação da API do Twenty não avançou.');
    }

    seenCursors.add(nextCursor);
    cursor = nextCursor;
  }

  throw new Error('A leitura excedeu o limite de segurança de 10.000 registros.');
};

export const parseTimelineDependency = (
  record: UnknownRecord,
): TimelineDependency => {
  const id = typeof record.id === 'string' ? record.id : null;
  const fromTaskId =
    typeof record.fromTaskId === 'string'
      ? record.fromTaskId
      : record.fromTask && typeof record.fromTask === 'object'
        ? String((record.fromTask as UnknownRecord).id ?? '')
        : '';
  const toTaskId =
    typeof record.toTaskId === 'string'
      ? record.toTaskId
      : record.toTask && typeof record.toTask === 'object'
        ? String((record.toTask as UnknownRecord).id ?? '')
        : '';

  if (!id || !fromTaskId || !toTaskId) {
    throw new Error('Um vínculo de tarefa está incompleto na API do Twenty.');
  }

  if (typeof record.relationType !== 'string') {
    throw new Error('Um vínculo de tarefa não possui relationType.');
  }

  const relationType = record.relationType.toUpperCase();

  if (relationType !== 'DEPENDS_ON' && relationType !== 'RELATED') {
    throw new Error(`Tipo de vínculo desconhecido: ${record.relationType}.`);
  }

  return {
    id,
    fromTaskId,
    toTaskId,
    relationType,
  };
};

export const loadTimelinePayload = async ({
  viewVariableName,
  includeDependencies = true,
}: {
  viewVariableName: string;
  includeDependencies?: boolean;
}): Promise<TimelinePayload> => {
  const viewId = getApplicationVariable(viewVariableName)?.trim();

  if (!viewId) {
    throw new Error(
      `Configure a variável ${viewVariableName} nas configurações do aplicativo.`,
    );
  }

  const metadataClient = new MetadataApiClient();
  const { getView } = await metadataClient.query({
    getView: {
      __args: { id: viewId },
      id: true,
      name: true,
      objectMetadataId: true,
      viewFilters: {
        id: true,
        fieldMetadataId: true,
        operand: true,
        value: true,
        viewFilterGroupId: true,
        subFieldName: true,
      },
      viewFilterGroups: {
        id: true,
        parentViewFilterGroupId: true,
        logicalOperator: true,
      },
    },
  });

  if (!getView) {
    throw new Error('A visão configurada não existe ou não está acessível.');
  }

  const { objects } = await metadataClient.query({
    objects: {
      __args: { paging: { first: 100 }, filter: {} },
      edges: {
        node: {
          id: true,
          nameSingular: true,
          fields: {
            __args: { paging: { first: 500 }, filter: {} },
            edges: { node: { id: true, name: true, type: true } },
          },
        },
      },
    },
  });
  const taskObject = objects.edges
    .map((edge) => edge.node)
    .find((object) => object.id === getView.objectMetadataId);

  if (!taskObject) {
    throw new Error('O objeto da visão configurada não foi encontrado.');
  }

  if (taskObject.nameSingular !== 'task') {
    throw new Error('A visão configurada precisa pertencer ao objeto Tarefa.');
  }

  const restClient = new RestApiClient();
  const [allTasks, dependencyRecords] = await Promise.all([
    fetchEveryRecord(restClient, '/rest/tasks'),
    includeDependencies
      ? fetchEveryRecord(restClient, '/rest/clickUpDependencies')
      : Promise.resolve([]),
  ]);
  const fields = taskObject.fields.edges.map(
    (edge) => edge.node as FieldMetadataLike,
  );
  const tasks = applyViewFilters(
    allTasks,
    fields,
    getView.viewFilters as ViewFilterLike[],
    getView.viewFilterGroups as ViewFilterGroupLike[],
  );

  return {
    tasks,
    dependencies: dependencyRecords.map(parseTimelineDependency),
    viewName: getView.name,
  };
};
