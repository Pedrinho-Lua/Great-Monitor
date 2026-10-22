# Monitor — Libby's Test Server

Aplicativo de desktop para Windows 11 que consulta o servidor Garry's Mod **Libby's Test Server** em `103.170.155.169:27017` pelos protocolos UDP Source A2S_INFO e A2S_PLAYER.

Um executável pronto está incluído em `dist\LibbysServerMonitor.exe`; a pasta também contém o código-fonte e as instruções para executar e gerar outro build.

## O que ele faz

- Consulta o servidor a cada 10 segundos por padrão; na aba **Customização**, o intervalo pode ser alterado de 5 a 300 segundos.
- Responde ao desafio A2S_INFO quando o servidor o exige e aceita respostas Source divididas em vários pacotes.
- Faz até 3 tentativas UDP por ciclo. Só marca o servidor como sem resposta depois de 3 ciclos consecutivos sem resposta válida.
- Só confirma a volta após 2 respostas seguidas.
- Mostra mapa, versão e horário da última verificação.
- A aba **Jogadores** mostra os nomes, pontuações e tempo conectado que o servidor compartilhar via A2S_PLAYER.
- A aba **Jogadores Ruims** mantém uma lista pessoal editável e destaca na aba de jogadores os nomes que você adicionou.
- Inclui uma aba **Console** que acompanha novas linhas do `console.txt` do Garry's Mod dentro da própria janela do aplicativo.
- Inclui uma aba **Histórico** com as mudanças confirmadas entre online e indisponível. Guarda até 500 eventos no computador.
- Permite personalizar na aba **Customização** as cores, a fonte e o tamanho do texto do console, além de quebra de linha e rolagem automática.
- Ativa por padrão o início automático para o usuário atual do Windows. É possível desmarcar essa opção na janela do app.
- Exibe o tempo da resposta A2S em milissegundos, atualizado a cada verificação bem-sucedida.

Uma falha de resposta pode resultar de perda de pacotes, roteamento ou filtro de rede. O app relata **sem resposta** após as verificações de confirmação; não consegue provar que o processo do servidor sofreu um crash.

## Executar a partir do código

1. Instale Python 3.10 ou mais recente para Windows. Durante a instalação, habilite **Add python.exe to PATH**.
2. Extraia a pasta `LibbysServerMonitor` em um local permanente.
3. Abra `run.bat` ou execute `python app.py` nessa pasta. O app usa apenas componentes incluídos no Python.

O app tenta ativar o início automático em `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. A opção pode ser alterada na janela e não exige privilégios de administrador.

## Usar a aba Console

O Garry's Mod só salva a saída do console em `console.txt` quando é iniciado com a opção `-condebug`.

1. Na Steam, abra **Biblioteca**, clique com o botão direito em **Garry's Mod** e escolha **Propriedades**.
2. Em **Geral > Opções de inicialização**, adicione `-condebug`.
3. Se o jogo já estava aberto, feche-o e abra-o novamente para aplicar a opção.
4. Abra a aba **Console** deste aplicativo e clique em **Selecionar arquivo de log**. O arquivo normalmente fica na pasta `garrysmod` da instalação do jogo e se chama `console.txt`.

O aplicativo acompanha esse arquivo em tempo real e lembra a seleção. A visualização fica limitada às 5.000 linhas mais recentes para manter a janela responsiva. O registro vem do console local do seu cliente do jogo e pode incluir as mensagens que ele recebe durante a partida; não dá acesso ao console privado do servidor dedicado nem aos consoles de outros jogadores. A opção `-condebug` faz o jogo gravar a saída do console em `console.txt`, conforme a [wiki oficial do Garry's Mod](https://wiki.facepunch.com/gmod/ConVars_In_Garrysmod).

## Personalizar o aplicativo

Na aba **Customização**, ajuste o intervalo do monitor entre 5 e 300 segundos. Para o console, escolha uma das fontes monoespaçadas disponíveis, tamanho de 8 a 20, cores de fundo e texto, quebra de linhas compridas e rolagem automática. As escolhas ficam guardadas no computador. O intervalo selecionado também aparece na aba **Monitor**.

Na aba **Histórico**, veja as mudanças de disponibilidade que o monitor confirmou após várias verificações consecutivas. O registro persiste entre aberturas do app, mantém as 500 mudanças mais recentes e pode ser apagado pelo botão **Limpar histórico**.

Na aba **Jogadores Ruims**, digite um nome para adicioná-lo à sua lista pessoal; selecione um registro e clique em **Remover selecionado** para tirá-lo. Os nomes são guardados neste computador. Isso não bane ninguém do servidor: apenas destaca correspondências na sua lista local.

## Criar um executável do Windows

Em um computador Windows com Python 3.10 ou mais recente e acesso à internet:

1. Abra `build_windows.bat`.
2. O script instala PyInstaller e cria `dist\LibbysServerMonitor.exe`.
3. Copie esse `.exe` para o local permanente desejado e execute-o. O app pode então ser marcado para iniciar com o Windows.

O executável é criado no próprio Windows, pois o empacotador gera um binário específico para a plataforma. O código-fonte continua disponível ao lado deste guia.

## Observações

- O app precisa permanecer aberto para continuar monitorando. Fechar a janela encerra as consultas; minimizá-la mantém o monitor ativo.
- A rede/firewall local precisa permitir tráfego UDP de saída para `103.170.155.169:27017` e as respostas correspondentes.
- Alguns servidores não respondem à consulta A2S_PLAYER ou ocultam a lista. Nessa situação, a aba **Jogadores** informa que não conseguiu atualizar os nomes; isso não altera o estado de disponibilidade do monitor.
- O histórico só registra mudanças de estado confirmadas; uma falha de pacote isolada não cria um evento.
- O monitor consulta somente informações públicas A2S_INFO e A2S_PLAYER; não tenta entrar no jogo, autenticar, banir jogadores ou alterar o servidor.
- O tempo A2S mede a duração completa da consulta bem-sucedida, incluindo desafio e eventuais novas tentativas; ele não é uma medição ICMP de ping.
- A implementação do desafio segue a descrição atualizada do protocolo publicada por [Valve](https://steamcommunity.com/discussions/forum/14/2974028351344359625/).

## Verificação local do protocolo

Execute `python -m unittest discover -s tests -v` para validar as respostas A2S_INFO e A2S_PLAYER, o desafio UDP, respostas divididas e a confirmação de mudanças de estado sem depender do servidor externo.
