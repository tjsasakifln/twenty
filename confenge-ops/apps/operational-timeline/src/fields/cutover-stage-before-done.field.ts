import {
  defineField,
  FieldType,
  MetadataWritability,
  STANDARD_OBJECT,
} from 'twenty-sdk/define';

export default defineField({
  universalIdentifier: '38c98da3-3395-458a-a9f6-eafbda739e50',
  objectUniversalIdentifier: STANDARD_OBJECT.task.universalIdentifier,
  type: FieldType.TEXT,
  name: 'cutoverStageBeforeDone',
  label: 'Etapa anterior (sistema)',
  description:
    'Estado interno persistido atomicamente para restaurar a etapa ao reabrir uma tarefa concluída.',
  isUIEditable: false,
  writability: MetadataWritability.APPLICATION,
});
