# Monitor — Libby's Test Server

Aplicativo de desktop para Windows 11 que consulta o servidor Garry's Mod **Libby's Test Server** em `103.170.155.169:27017` pelos protocolos UDP Source A2S_INFO e A2S_PLAYER.

Um executável pronto está incluído em `dist\LibbysServerMonitor.exe`. A mesma pasta contém Português e Inglês: o app inicia em Português e você pode trocar o idioma na aba **Customização**, sem manter pastas separadas.

## O que ele faz

- Consulta o servidor a cada 10 segundos por padrão. Sem o modo de desenvolvedor, o intervalo pode ser alterado de 5 a 300 segundos; com ele ativado, de 1 segundo até o limite aceito pelo Python no Windows.
- Responde ao desafio A2S_INFO quando o servidor o exige e aceita respostas Source divididas em vários pacotes.
- Faz até 3 tentativas UDP por ciclo. Só marca o servidor como sem resposta depois de 3 ciclos consecutivos sem resposta válida.
- Só confirma a volta após 2 respostas seguidas.
- Evita disparar checagens em sequência quando uma consulta demora mais que o intervalo configurado. A lista de jogadores é consultada no máximo uma vez a cada 10 segundos e a última lista fica visível durante falhas breves.
- Mostra mapa, versão e horário da última verificação.
- A aba **Jogadores** mostra os nomes, pontuações e tempo conectado que o servidor compartilhar via A2S_PLAYER.
- A aba **Jogadores Ruims** mantém uma lista pessoal editável e destaca na aba de jogadores os nomes que você adicionou.
- Inclui uma aba **Console** que acompanha novas linhas do arquivo de log do console do Garry's Mod dentro da própria janela do aplicativo.
- Inclui uma aba **Histórico** com as mudanças confirmadas entre online e indisponível. Guarda até 500 eventos no computador e permite escolher quais tipos de mudança registrar ou desativar novos registros.
- **Modo de desenvolvedor** fica desativado por padrão na aba **Customização** e libera as opções avançadas. Com ele ativado, marque **Ativar console de desenvolvedor** para mostrar a aba **Desenvolvedor**, que guarda até 3.000 eventos recentes sem incluir o texto do chat.
- Permite personalizar na aba **Customização** as cores, a fonte e o tamanho do texto do console, além de quebra de linha e rolagem automática.
- Ativa por padrão o início automático para o usuário atual do Windows. É possível desmarcar essa opção na janela do app.
- Exibe o tempo da resposta A2S em milissegundos, atualizado a cada verificação bem-sucedida.
- Permite adicionar até 100 servidores na aba **Servidores** e consulta todos em paralelo. Selecione um item da lista para ver os detalhes, jogadores e histórico daquele servidor nas outras abas.
- Aceita endereço IPv4 ou domínio com porta, incluindo endereços Playit, como `domination-section.tun.ply.gg:45742`.

Uma falha de resposta pode resultar de perda de pacotes, roteamento ou filtro de rede. O app relata **sem resposta** após as verificações de confirmação; não consegue provar que o processo do servidor sofreu um crash.

## Executar a partir do código

1. Instale Python 3.10 ou mais recente para Windows. Durante a instalação, habilite **Add python.exe to PATH**.
2. Extraia a pasta `LibbysServerMonitor` em um local permanente.
3. Abra `run.bat` ou execute `python app.py` nessa pasta. O app usa apenas componentes incluídos no Python.

O app tenta ativar o início automático em `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. A opção pode ser alterada na janela e não exige privilégios de administrador.

## Usar a aba Console

O Garry's Mod pode salvar a saída do console em um arquivo como `console.txt` com a opção `-condebug`; algumas configurações usam um arquivo `.log`.

1. Na Steam, abra **Biblioteca**, clique com o botão direito em **Garry's Mod** e escolha **Propriedades**.
2. Em **Geral > Opções de inicialização**, adicione `-condebug`.
3. Se o jogo já estava aberto, feche-o e abra-o novamente para aplicar a opção.
4. Abra a aba **Console** deste aplicativo e clique em **Selecionar arquivo de log**. Escolha o arquivo que seu jogo gera, por exemplo `console.log` ou `console.txt`, normalmente na pasta `garrysmod`.

O aplicativo acompanha esse arquivo em tempo real e lembra a seleção. A visualização fica limitada às 5.000 linhas mais recentes para manter a janela responsiva. O registro vem do console local do seu cliente do jogo e pode incluir as mensagens que ele recebe durante a partida; não dá acesso ao console privado do servidor dedicado nem aos consoles de outros jogadores. A opção `-condebug` grava a saída em `console.txt` por padrão, conforme a [wiki oficial do Garry's Mod](https://wiki.facepunch.com/gmod/ConVars_In_Garrysmod).

## Personalizar o aplicativo

Na aba **Servidores**, informe um nome e um endereço IPv4 ou domínio com porta e clique em **Adicionar**. Cada servidor continua sendo verificado em paralelo; selecione-o na lista para mostrar seus dados nas abas **Monitor**, **Jogadores** e **Histórico**. Por exemplo, você pode adicionar `domination-section.tun.ply.gg:45742` para monitorar o endereço Playit. A lista aceita até 100 servidores. Para alterar o nome ou endereço do selecionado, use a seção **Servidor para monitorar** da aba **Customização** e clique em **Aplicar e salvar**.

Na aba **Customização**, escolha **Português** ou **Inglês** na seção **Idioma**. Português é o padrão. Depois de clicar em **Aplicar e salvar**, o aplicativo reinicia automaticamente no idioma selecionado. O nome e o endereço nessa aba pertencem ao servidor selecionado; o endereço aceita IPv4 ou domínio e porta. Sem o modo de desenvolvedor, o intervalo fica entre 5 e 300 segundos e vale para todos os servidores. Ao ativá-lo, o mínimo passa a 1 segundo e o máximo acompanha o limite de espera suportado pelo Python no Windows. Marque **Ativar modo de desenvolvedor** para liberar esse intervalo ampliado. Marque também **Ativar console de desenvolvedor** para mostrar ou ocultar a aba **Desenvolvedor**. Para o console, escolha uma das fontes monoespaçadas disponíveis, tamanho de 8 a 20, cores de fundo e texto, quebra de linhas compridas e rolagem automática. A aba registra trocas de aba, consultas A2S, mudanças de configuração e atualizações dos arquivos acompanhados. As escolhas ficam guardadas no computador.

Na aba **Customização**, use **Ativar histórico** para parar ou retomar o registro de novas mudanças. Em **Filtrar histórico**, escolha independentemente se quer registrar quando o servidor fica indisponível e quando volta a ficar online; deixe ambos marcados para registrar tudo. Essas opções afetam apenas novos registros e preservam os anteriores. Na aba **Histórico**, veja até as 500 mudanças mais recentes ou apague-as pelo botão **Limpar histórico**.

Na aba **Jogadores Ruims**, digite um nome para adicioná-lo à sua lista pessoal; selecione um registro e clique em **Remover selecionado** para tirá-lo. Os nomes são guardados neste computador. Isso não bane ninguém do servidor: apenas destaca correspondências na sua lista local. A pontuação é lida como número inteiro e a coluna **Índice** é o índice A2S enviado pelo servidor, não um SteamID.

## Criar um executável do Windows

Em um computador Windows com Python 3.10 ou mais recente e acesso à internet:

1. Abra `build_windows.bat`.
2. O script instala PyInstaller e cria `dist\LibbysServerMonitor.exe`, com os dois idiomas incluídos.
3. Copie esse `.exe` para o local permanente desejado e execute-o. O app pode então ser marcado para iniciar com o Windows.

O executável é criado no próprio Windows, pois o empacotador gera um binário específico para a plataforma. Os arquivos `app_pt.py` e `app_en.py` contêm os textos de cada idioma; `app.py` escolhe qual versão abrir usando a preferência salva. Todo o código-fonte continua disponível ao lado deste guia.

## Observações

- O app precisa permanecer aberto para continuar monitorando. Fechar a janela encerra as consultas; minimizá-la mantém o monitor ativo.
- A rede/firewall local precisa permitir tráfego UDP de saída para `103.170.155.169:27017` e as respostas correspondentes.
- Todos os servidores da lista são consultados enquanto o aplicativo está aberto; o servidor selecionado controla os detalhes exibidos nas outras abas. Para um endereço Playit, adicione o domínio e a porta de consulta A2S na aba **Servidores**.
- Alguns servidores não respondem à consulta A2S_PLAYER ou ocultam a lista. Nessa situação, a aba **Jogadores** informa que não conseguiu atualizar os nomes; isso não altera o estado de disponibilidade do monitor.
- O histórico só registra mudanças de estado confirmadas; uma falha de pacote isolada não cria um evento.
- O monitor consulta somente informações públicas A2S_INFO e A2S_PLAYER; não tenta entrar no jogo, autenticar, banir jogadores ou alterar o servidor.
- O tempo A2S mede a duração completa da consulta bem-sucedida, incluindo desafio e eventuais novas tentativas; ele não é uma medição ICMP de ping.
- A implementação do desafio segue a descrição atualizada do protocolo publicada por [Valve](https://steamcommunity.com/discussions/forum/14/2974028351344359625/).

## Verificação local do protocolo

Execute `python -m unittest discover -s tests -v` para validar as respostas A2S_INFO e A2S_PLAYER, o desafio UDP, respostas divididas e a confirmação de mudanças de estado sem depender do servidor externo.
"# Great-Monitor" 
