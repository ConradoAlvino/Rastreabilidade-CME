**Protótipo local de rastreabilidade de kits hospitalares.**

##  Arsenal compartilhado

- O **Arsenal** é a localização inicial e de retorno das bandejas. Nenhuma bandeja nasce vinculada à Enfermaria, ao CTI, ao Pronto Atendimento ou ao Bloco Cirúrgico.
- Um banco novo inicia com 30 bandejas fictícias pré-montadas, em 3 padrões de composição, dez bandejas por padrão. Cada uma possui 13 unidades de materiais, distribuídas em 10 tipos, apenas para simulação. Os padrões usam dois blocos de identificação, com 4 cores exclusivas por bloco (16 combinações possíveis).
- O médico permanece vinculado ao **seu setor de atendimento** e solicita um padrão específico de composição/cores, disponível no Arsenal. O pedido fica ligado ao médico e ao setor, mas a bandeja **não é permanentemente reservada** àquele setor.
- O gerente de estoque separa uma bandeja compatível e registra a saída do Arsenal. Até ser recebida, sua localização aparece como **Em transporte**.
- No **check-in feito pelo técnico de enfermagem do setor solicitante**, após vincular um paciente daquele setor, a localização física da bandeja passa para o **setor de atendimento**.
- Na devolução, a bandeja é enviada ao Arsenal. **Somente após a confirmação do recebimento pelo gerente** ela volta ao estado `estoque` e à localização **Arsenal**. Se houver falta de itens ou ocorrência de lacre, continua bloqueada, mesmo fisicamente no Arsenal.
- Os alertas de estoque baixo consideram o **padrão de cores**, não um estoque separado por setor. Um médico pode visualizar o mesmo quantitativo de kits compartilhados que outros setores, porque todos consultam um pool único.

Os demais recursos da versão anterior foram preservados: cadastro/login/aprovação, perfis, solicitações, pacientes fictícios, checklist pré-marcada por unidade (com nome repetido em cada checkbox), observações, lacre violado, fotografias, empréstimos, transferências entre médicos com aceite, temporizador iniciado no check-in, históricos, pendências e inventário rotativo.

## Executar no Windows

```powershell
python .\web_app.py
```

Abra <http://127.0.0.1:8000>. O programa cria automaticamente `bandejas_web.db` se ele não existir.

## Testes

```powershell
python -m unittest discover -s tests -v
```

O arquivo `tests/test_versao_final.py` verifica o banco inicial, a ausência de setor fixo, a solicitação multissetorial, o check-in, o retorno ao Arsenal, a não duplicação de kits e a migração de dados antigos.

## Funções 

Permissões de cada classe de usuário
- Médico: solicita bandejas conforme a composição desejada, acompanha a disponibilidade e os prazos de devolução e pode transferir a responsabilidade para outros médicos.
- Técnico de enfermagem: visualiza solicitações do setor, cadastra pacientes, realiza check-in e check-out, confere os materiais e registra itens faltantes, danos e observações.
- Gerente de estoque (Arsenal): cadastra bandejas e composições, gerencia o estoque, libera solicitações, recebe devoluções, regulariza pendências e acompanha inventários e históricos.