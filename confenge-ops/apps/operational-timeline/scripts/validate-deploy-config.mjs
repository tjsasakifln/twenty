const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const customObjectIds = (
  process.env.CONFENGE_TIMELINE_READ_OBJECT_UNIVERSAL_IDENTIFIERS ?? ''
)
  .split(',')
  .map((value) => value.trim())
  .filter(Boolean);

if (
  customObjectIds.length !== 2 ||
  customObjectIds.some((id) => !uuidPattern.test(id))
) {
  throw new Error(
    'CONFENGE_TIMELINE_READ_OBJECT_UNIVERSAL_IDENTIFIERS deve conter exatamente os UUIDs de clickUpDependency e clickUpList, separados por vírgula.',
  );
}

if (!process.env.TWENTY_DEPLOY_API_KEY?.trim()) {
  throw new Error('TWENTY_DEPLOY_API_KEY não está carregada no processo de deploy.');
}

console.log('Configuração privada de deploy validada (valores omitidos).');
