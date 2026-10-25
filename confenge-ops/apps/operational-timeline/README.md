# Cronograma e agenda operacional (Twenty 2.45)

Aplicativo nativo do Twenty que substitui as visões temporais usadas no
ClickUp sem criar outro portal ou outro login.

## Entregas

- **Cronograma operacional:** barras somente quando a tarefa tem início e fim;
  losango quando há uma única data; hierarquia de pai/subtarefa e dependências
  explícitas; clique abre a tarefa nativa para editar.
- **Agenda operacional:** grade mensal e lista semanal combinando
  `clickUpDueOn` (dia inteiro) com `dueAt` (horário em Brasília). Se um registro
  histórico tiver os dois campos, `dueAt` prevalece e a tarefa aparece uma vez.
- **Cutover nativo:** eventos `tasks.created` e `tasks.updated` aplicam defaults
  e conciliam Etapa/status e as duas representações de data com compare-and-swap.

Os dois componentes leem os filtros das visões por ID em tempo de execução. O
nome da visão pode mudar sem quebrar o aplicativo, e nenhum ID de registro do
workspace fica versionado.

## Compatibilidade confirmada

- `twenty-sdk` e `twenty-client-sdk`: **2.45.0**
- Node.js: `^24.5.0`
- Yarn: `>=4.0.2` (lockfile Yarn 4 versionado)
- Recursos usados: `STANDALONE_PAGE`, widget `FRONT_COMPONENT`, navegação
  `PAGE_LAYOUT`, `RestApiClient`, `MetadataApiClient`, variáveis de aplicação e
  gatilhos `databaseEventTriggerSettings`.

## Configuração privada

Antes de `plan` ou `apply`, o processo de build precisa receber, sem registrar
os valores em log:

```bash
export CONFENGE_TIMELINE_READ_OBJECT_UNIVERSAL_IDENTIFIERS="<uid-clickUpDependency>,<uid-clickUpList>"
```

Isso materializa no manifesto apenas leitura dos dois objetos customizados. A
role nunca concede `read-all` ou `write-all`: lê/atualiza Task, lê os objetos
customizados informados acima e não exclui registros.

Depois de instalar/sincronizar, preencher as variáveis do aplicativo no
workspace:

| Variável | Tipo | Exposição | Uso |
|---|---|---|---|
| `CONTRACTUAL_MILESTONES_VIEW_ID` | TEXT | front component autenticado | filtro do cronograma |
| `OPERATIONAL_CALENDAR_VIEW_ID` | TEXT | front component autenticado | filtro da agenda |
| `CUTOVER_ENABLED` | BOOLEAN | configuração | gate; padrão `false` |
| `CUTOVER_MEMBER_ID` | TEXT | secret | responsável padrão |
| `CUTOVER_DEFAULT_LIST_ID` | TEXT | secret | lista padrão |
| `CUTOVER_STAGE_STATUS_MAP` | RAW_JSON | secret | mapa Etapa → `TODO`/`IN_PROGRESS`/`DONE` |
| `CUTOVER_STAGE_TODO` | TEXT | secret | Etapa canônica de TODO |
| `CUTOVER_STAGE_IN_PROGRESS` | TEXT | secret | Etapa canônica em andamento |
| `CUTOVER_STAGE_DONE` | TEXT | secret | Etapa canônica concluída |

Exemplo de forma do mapa, usando chaves simbólicas (não copie estes valores):

```json
{
  "<option-id-etapa-a-fazer>": "TODO",
  "<option-id-etapa-em-andamento>": "IN_PROGRESS",
  "<option-id-etapa-concluida>": "DONE"
}
```

## Validação local

Executar a partir desta pasta:

```bash
corepack enable
yarn install --immutable
yarn test
yarn typecheck
yarn build
```

`yarn build` executa o comando correto do SDK 2.45: `twenty dev:build`. A saída
local fica em `.twenty/output` e não é versionada.

## Plano e deploy coordenado

O deploy usa API key lida somente no host; não requer credencial no navegador.
Os comandos exatos, depois do backup e com as variáveis privadas já exportadas,
são:

```bash
yarn deploy:check

yarn twenty remote:add --as confenge-production \
  --url https://gestao.confenge.com.br \
  --api-key "$TWENTY_DEPLOY_API_KEY"

yarn twenty --remote confenge-production plan . --no-delete
# Revisar integralmente o plano antes de continuar.
yarn twenty --remote confenge-production apply . --no-delete
```

Não executar `apply` se o plano remover entidades, ampliar a role ou se
`CONFENGE_TIMELINE_READ_OBJECT_UNIVERSAL_IDENTIFIERS` estiver vazio. Depois do
`apply`, manter `CUTOVER_ENABLED=false`, preencher e validar as outras oito
variáveis, abrir Cronograma/Agenda com um usuário real e só então mudar
`CUTOVER_ENABLED=true`. Mesmo com os triggers registrados na fase inicial, o
gate retorna sem carregar variáveis nem escrever tarefas.

## Comportamento seguro

- Datas `YYYY-MM-DD` são tratadas como datas civis, sem conversão UTC.
- `DATE_TIME` é posicionado e apresentado em `America/Sao_Paulo`.
- Uma única data nunca vira duração artificial.
- Paginação usa `data + pageInfo`, segue `starting_after` e aborta em cursor
  repetido.
- Filtros desconhecidos falham fechados com erro visível em vez de mostrar
  registros fora do escopo.
- Edição de datas, pai e dependências continua na interface nativa da tarefa.
