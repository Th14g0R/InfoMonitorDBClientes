/**
 * backups_ftp_admin.js
 * Painel Administrativo de Backups FTP e Listas Cobian (Apenas Usuários Master)
 *
 * Regras estritas de segurança e integridade:
 * 1. Zero senhas ou dados sensíveis no HTML inicial.
 * 2. Manipulação de DOM estritamente segura usando createElement e textContent (PROIBIDO innerHTML com dados dinâmicos).
 * 3. Requisições POST/PUT/DELETE com cabeçalho X-CSRF-Token.
 * 4. Revelação de senha e extras NUNCA preenche inputs editáveis; utiliza spans read-only dedicados por 20s.
 * 5. Campos extras confidenciais usam type="password", e chaves já salvas são estritamente readOnly.
 * 6. Identificador de geração (generationId) e checagem de idEsperado em cada requisição/transição
 *    (abrir/fechar modal, abrir form novo/edição, cancelar, alternar listas) para anular responses
 *    atrasadas e evitar race condition entre clientes e entre rotinas.
 * 7. Fallback de clipboard seguro com try/finally, limpeza de memória da textarea e verificação do retorno de execCommand.
 * 8. Carregamento de clientes administrativos via GET /admin/backups-ftp/clientes para cobrir clientes fora do feed.
 */

(function () {
    'use strict';

    // Identificador de geração para descartar respostas de rede desatualizadas (race conditions)
    let geracaoRequisicao = 0;

    // Estado da sessão do painel
    let aliasAtual = '';
    let rotinaAtual = null;
    let chavesExtrasOriginais = [];
    let timerMascaraSenhaForm = null;

    // Elementos do DOM
    const modal = document.getElementById('modal-backups-ftp-admin');
    const modalAlias = document.getElementById('ftp-modal-alias');
    const alertaBox = document.getElementById('ftp-admin-alerta');
    const btnFecharModal = document.getElementById('btn-fechar-modal-ftp');
    const selectCliente = document.getElementById('ftp-select-cliente');
    const btnNovoClienteManual = document.getElementById('btn-novo-cliente-manual');

    // Visão 1: Rotinas
    const visaoRotinas = document.getElementById('ftp-visao-rotinas');
    const containerCardsRotinas = document.getElementById('ftp-container-cards-rotinas');
    const btnCriarNovaRotina = document.getElementById('btn-criar-nova-rotina');

    // Visão 2: Form
    const visaoFormRotina = document.getElementById('ftp-visao-form-rotina');
    const formTitulo = document.getElementById('ftp-form-titulo');
    const campoRotinaId = document.getElementById('ftp-rotina-id');
    const campoRevision = document.getElementById('ftp-rotina-revision');
    const campoNome = document.getElementById('ftp-campo-nome');
    const campoServidor = document.getElementById('ftp-campo-servidor');
    const campoPorta = document.getElementById('ftp-campo-porta');
    const campoUsuario = document.getElementById('ftp-campo-usuario');
    const campoSenha = document.getElementById('ftp-campo-senha');
    const btnRevelarSenhaForm = document.getElementById('btn-revelar-senha-form');
    const btnCopiarSenhaForm = document.getElementById('btn-copiar-senha-form');
    const senhaReveladaBox = document.getElementById('ftp-senha-revelada-box');
    const senhaReveladaTexto = document.getElementById('ftp-senha-revelada-texto');
    const senhaStatusInfo = document.getElementById('ftp-senha-status-info');
    const lblRemoverSenha = document.getElementById('lbl-remover-senha');
    const chkRemoverSenha = document.getElementById('ftp-chk-remover-senha');
    const campoResponsavel = document.getElementById('ftp-campo-responsavel');
    const campoLancamento = document.getElementById('ftp-campo-lancamento');
    const campoNotas = document.getElementById('ftp-campo-notas');
    const containerExtras = document.getElementById('ftp-container-extras');
    const btnAddCampoExtra = document.getElementById('btn-add-campo-extra');
    const btnCancelarForm = document.getElementById('btn-cancelar-form-rotina');
    const btnSalvarRotina = document.getElementById('btn-salvar-rotina');

    // Visão 3: Listas Cobian
    const visaoListas = document.getElementById('ftp-visao-listas-cobian');
    const btnFecharVisaoListas = document.getElementById('btn-fechar-visao-listas');
    const inputArquivoList = document.getElementById('ftp-input-arquivo-list');
    const inputComentarioList = document.getElementById('ftp-input-comentario-list');
    const btnEnviarArquivoList = document.getElementById('btn-enviar-arquivo-list');
    const containerTabelaVersoes = document.getElementById('ftp-tabela-versoes-container');

    // Sub-modal: Ver Conteúdo Cobian
    const submodalConteudo = document.getElementById('modal-sub-ver-conteudo-list');
    const submodalTitulo = document.getElementById('submodal-list-titulo');
    const preConteudoList = document.getElementById('ftp-pre-conteudo-list');
    const btnFecharSubmodal = document.getElementById('btn-fechar-submodal-conteudo');
    const btnFecharSubmodalRodape = document.getElementById('btn-fechar-submodal-conteudo-rodape');

    if (!modal) {
        return; // Não é usuário Master ou template não injetado
    }

    // -------------------------------------------------------------------------
    // Helpers de Segurança e Requisições
    // -------------------------------------------------------------------------

    function obterCsrf() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    function mostrarAlerta(msg, tipo = 'erro') {
        if (!alertaBox) return;
        alertaBox.textContent = msg;
        alertaBox.style.display = 'block';
        if (tipo === 'erro') {
            alertaBox.style.background = '#fef2f2';
            alertaBox.style.border = '1px solid #fecaca';
            alertaBox.style.color = '#991b1b';
        } else {
            alertaBox.style.background = '#f0fdf4';
            alertaBox.style.border = '1px solid #bbf7d0';
            alertaBox.style.color = '#166534';
        }
    }

    function limparAlerta() {
        if (!alertaBox) return;
        alertaBox.textContent = '';
        alertaBox.style.display = 'none';
    }

    async function apiFetch(url, options = {}) {
        const headers = options.headers || {};
        const method = (options.method || 'GET').toUpperCase();
        if (method !== 'GET') {
            headers['X-CSRF-Token'] = obterCsrf();
        }
        options.headers = headers;
        options.credentials = 'same-origin';

        try {
            const resp = await fetch(url, options);

            // Detecta redirecionamento para tela de login (sessão expirada ou deslogada)
            if (resp.redirected || (resp.url && resp.url.includes('/admin/login'))) {
                fecharModalAdmin();
                window.location.href = resp.url || '/admin/login?next=/backups-ftp';
                return { ok: false, status: 302, erro: 'Sessão expirada. Redirecionando para login...' };
            }

            const data = await resp.json().catch(() => null);

            if (!resp.ok) {
                const erroMsg = (data && data.erro) ? data.erro : `Erro na requisição (${resp.status})`;
                return { ok: false, status: resp.status, erro: erroMsg, data };
            }
            return { ok: true, status: resp.status, data };
        } catch (err) {
            return { ok: false, status: 0, erro: 'Falha de comunicação com o servidor.', detalhe: err };
        }
    }

    function limparTimerSenhaForm() {
        if (timerMascaraSenhaForm) {
            clearTimeout(timerMascaraSenhaForm);
            timerMascaraSenhaForm = null;
        }
        if (senhaReveladaBox) senhaReveladaBox.style.display = 'none';
        if (senhaReveladaTexto) senhaReveladaTexto.textContent = '';
    }

    async function copiarTextoClipboard(texto, mensagemSucesso) {
        if (!texto) {
            mostrarAlerta('Nenhum texto disponível para cópia.', 'erro');
            return;
        }

        // Tenta Clipboard API moderno se disponível e em contexto seguro
        if (navigator.clipboard && navigator.clipboard.writeText) {
            try {
                await navigator.clipboard.writeText(texto);
                mostrarAlerta(mensagemSucesso || 'Copiado para a área de transferência! (Auditado)', 'sucesso');
                return;
            } catch (err) {
                // Falha permitida: cai no fallback seguro de execCommand
            }
        }

        // Fallback seguro usando textarea temporária com try/finally
        const ta = document.createElement('textarea');
        ta.value = texto;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        ta.style.top = '-9999px';
        ta.setAttribute('readonly', '');
        document.body.appendChild(ta);

        let copiado = false;
        try {
            ta.select();
            copiado = document.execCommand('copy');
        } catch (e) {
            copiado = false;
        } finally {
            ta.value = ''; // Limpeza imediata do segredo
            if (ta.parentNode) {
                ta.parentNode.removeChild(ta);
            }
        }

        if (copiado) {
            mostrarAlerta(mensagemSucesso || 'Copiado para a área de transferência! (Auditado)', 'sucesso');
        } else {
            mostrarAlerta('Não foi possível copiar automaticamente para o clipboard. Permissão negada pelo navegador.', 'erro');
        }
    }

    // -------------------------------------------------------------------------
    // Gestão de Clientes Administrativos (União Feed + Banco SQLite)
    // -------------------------------------------------------------------------

    async function carregarListaClientesCompleta(aliasParaSelecionar) {
        if (!selectCliente) return;

        const res = await apiFetch('/admin/backups-ftp/clientes');
        if (!res.ok) return;

        const clientes = (res.data && res.data.clientes) ? res.data.clientes : [];
        selectCliente.textContent = '';

        let selecionado = aliasParaSelecionar;
        if (!selecionado && clientes.length > 0) {
            selecionado = clientes[0];
            aliasAtual = selecionado;
            if (modalAlias) modalAlias.textContent = aliasAtual;
            carregarRotinasCliente(aliasAtual);
        }

        clientes.forEach(aliasItem => {
            const opt = document.createElement('option');
            opt.value = aliasItem;
            opt.textContent = aliasItem;
            if (aliasItem === selecionado) {
                opt.selected = true;
            }
            selectCliente.appendChild(opt);
        });

        if (selecionado && !clientes.includes(selecionado)) {
            const optCustom = document.createElement('option');
            optCustom.value = selecionado;
            optCustom.textContent = `${selecionado} (Manual)`;
            optCustom.selected = true;
            selectCliente.appendChild(optCustom);
        }
    }

    // -------------------------------------------------------------------------
    // Abertura e Fechamento de Modais
    // -------------------------------------------------------------------------

    function abrirModalAdmin(alias) {
        // Incrementa generationId para descartar requisições pendentes anteriores
        geracaoRequisicao++;

        aliasAtual = (alias || '').trim();
        rotinaAtual = null;
        chavesExtrasOriginais = [];

        limparAlerta();
        limparTimerSenhaForm();

        if (modalAlias) {
            modalAlias.textContent = aliasAtual;
        }

        // Exibe Visão 1 e oculta as demais
        visaoRotinas.style.display = 'block';
        visaoFormRotina.style.display = 'none';
        visaoListas.style.display = 'none';

        modal.style.display = 'flex';
        modal.setAttribute('aria-hidden', 'false');

        // Carrega combobox de clientes e rotinas do cliente selecionado
        carregarListaClientesCompleta(aliasAtual);
        carregarRotinasCliente(aliasAtual);
    }

    function fecharModalAdmin() {
        geracaoRequisicao++; // Invalida qualquer resposta assíncrona atrasada
        limparTimerSenhaForm();
        limparAlerta();

        aliasAtual = '';
        rotinaAtual = null;
        chavesExtrasOriginais = [];

        // Limpeza estrita de qualquer segredo/conteúdo no DOM
        if (preConteudoList) preConteudoList.textContent = '';
        if (containerCardsRotinas) containerCardsRotinas.textContent = '';
        if (containerExtras) containerExtras.textContent = '';
        if (campoSenha) campoSenha.value = '';

        modal.style.display = 'none';
        modal.setAttribute('aria-hidden', 'true');
        fecharSubmodalList();
    }

    function fecharSubmodalList() {
        if (submodalConteudo) {
            submodalConteudo.style.display = 'none';
        }
        if (preConteudoList) {
            preConteudoList.textContent = '';
        }
    }

    // -------------------------------------------------------------------------
    // Visão 1: Listagem de Rotinas
    // -------------------------------------------------------------------------

    async function carregarRotinasCliente(alias) {
        const genAtual = geracaoRequisicao;
        limparAlerta();
        containerCardsRotinas.textContent = 'Carregando rotinas configuradas...';

        const res = await apiFetch(`/admin/backups-ftp/cliente/${encodeURIComponent(alias)}/rotinas`);

        // Proteção contra race condition: descarta se a geração mudou ou o cliente foi trocado
        if (genAtual !== geracaoRequisicao || alias !== aliasAtual) {
            return;
        }

        containerCardsRotinas.textContent = '';

        if (!res.ok) {
            mostrarAlerta(res.erro || 'Falha ao carregar rotinas.', 'erro');
            return;
        }

        const rotinas = (res.data && res.data.rotinas) ? res.data.rotinas : [];
        if (rotinas.length === 0) {
            const pVazio = document.createElement('p');
            pVazio.style.color = 'var(--cor-texto-suave, #64748b)';
            pVazio.style.fontSize = '0.9em';
            pVazio.textContent = 'Nenhuma rotina configurada para este cliente ainda. Clique em "➕ Nova Rotina" acima para cadastrar.';
            containerCardsRotinas.appendChild(pVazio);
            return;
        }

        rotinas.forEach(rotina => {
            const card = document.createElement('div');
            card.className = 'ftp-rotina-card';

            // Topo do Card
            const cardTopo = document.createElement('div');
            cardTopo.className = 'ftp-rotina-card-topo';

            const tituloBox = document.createElement('div');
            const h4 = document.createElement('h4');
            h4.textContent = rotina.nome_rotina || 'Rotina Sem Nome';
            tituloBox.appendChild(h4);

            const badgeStatus = document.createElement('span');
            badgeStatus.className = 'badge-status ' + (rotina.status === 1 ? 'badge-OK' : 'badge-SEM_ARQUIVO');
            badgeStatus.textContent = rotina.status === 1 ? 'Ativa' : 'Inativa';
            tituloBox.appendChild(badgeStatus);
            cardTopo.appendChild(tituloBox);

            // Ações do Topo
            const topoAcoes = document.createElement('div');
            topoAcoes.style.display = 'flex';
            topoAcoes.style.gap = '6px';

            const btnEditar = document.createElement('button');
            btnEditar.type = 'button';
            btnEditar.className = 'btn btn-sm';
            btnEditar.textContent = '✏ Editar';
            btnEditar.addEventListener('click', () => abrirFormEditarRotina(rotina));
            topoAcoes.appendChild(btnEditar);

            const btnExcluir = document.createElement('button');
            btnExcluir.type = 'button';
            btnExcluir.className = 'btn btn-sm btn-perigo';
            btnExcluir.textContent = '🗑 Excluir';
            btnExcluir.addEventListener('click', () => excluirRotina(rotina));
            topoAcoes.appendChild(btnExcluir);

            cardTopo.appendChild(topoAcoes);
            card.appendChild(cardTopo);

            // Grid de Detalhes
            const grid = document.createElement('div');
            grid.className = 'ftp-rotina-detalhes-grid';

            function addCampo(rotulo, valor) {
                const item = document.createElement('div');
                item.className = 'ftp-rotina-detalhes-item';
                const spanRot = document.createElement('span');
                spanRot.className = 'rotulo';
                spanRot.textContent = rotulo;
                const spanVal = document.createElement('span');
                spanVal.className = 'valor';
                spanVal.textContent = (valor !== undefined && valor !== null && String(valor).trim() !== '')
                    ? String(valor)
                    : (valor === 0 || valor === '0' ? '0' : '—');
                item.appendChild(spanRot);
                item.appendChild(spanVal);
                grid.appendChild(item);
            }

            addCampo('Servidor:', rotina.servidor ? `${rotina.servidor}:${rotina.porta || 21}` : '—');
            addCampo('Usuário:', rotina.usuario || '—');
            addCampo('Responsável:', rotina.responsavel || '—');
            addCampo('Lançamento:', rotina.lancamento_em || '—');

            // Campos Extras diretamente no grid via addCampo
            const mapaSensiveis = rotina.extras_sensiveis || {};
            const dadosExtrasObj = rotina.dados_extras || {};
            const chavesExtras = (Array.isArray(rotina.extras_configurados) && rotina.extras_configurados.length > 0)
                ? rotina.extras_configurados
                : Object.keys(dadosExtrasObj);

            chavesExtras.forEach(ch => {
                const ehSensivel = (typeof mapaSensiveis[ch] === 'boolean') ? mapaSensiveis[ch] : true;
                if (ehSensivel) {
                    addCampo(`${ch}:`, '•••••••• (Confidencial)');
                } else {
                    const valClaro = dadosExtrasObj[ch];
                    const valFormatado = (valClaro !== undefined && valClaro !== null && String(valClaro).trim() !== '')
                        ? String(valClaro)
                        : (valClaro === 0 || valClaro === '0' ? '0' : '—');
                    addCampo(`${ch}:`, valFormatado);
                }
            });

            card.appendChild(grid);

            // Bloco de Senha no Card
            const blocoSenha = document.createElement('div');
            blocoSenha.className = 'ftp-bloco-senha';

            const spanInfoSenha = document.createElement('span');
            spanInfoSenha.style.fontSize = '0.85em';
            spanInfoSenha.style.color = 'var(--cor-texto-suave, #64748b)';

            if (rotina.senha_configurada) {
                spanInfoSenha.textContent = '🔑 Senha: •••••••• (Criptografada no banco)';
                blocoSenha.appendChild(spanInfoSenha);

                // Span read-only isolado para senha revelada temporariamente no card
                const spanValorRevelado = document.createElement('span');
                spanValorRevelado.style.display = 'none';
                spanValorRevelado.style.fontFamily = 'monospace';
                spanValorRevelado.style.fontWeight = 'bold';
                spanValorRevelado.style.color = 'var(--cor-primaria, #2563eb)';
                spanValorRevelado.style.userSelect = 'all';
                blocoSenha.appendChild(spanValorRevelado);

                const acoesSenha = document.createElement('div');
                acoesSenha.style.display = 'flex';
                acoesSenha.style.gap = '6px';

                let timerCardSenha = null;

                const btnRevelar = document.createElement('button');
                btnRevelar.type = 'button';
                btnRevelar.className = 'btn btn-sm';
                btnRevelar.textContent = '👁 Revelar';
                btnRevelar.addEventListener('click', async () => {
                    const genClique = geracaoRequisicao;
                    const idEsperado = rotina.id;
                    const aliasEsperado = aliasAtual;
                    const resRev = await apiFetch(`/admin/backups-ftp/rotina/${idEsperado}/revelar-senha`, { method: 'POST' });

                    // Descarta se houve transição de tela ou troca de cliente
                    if (genClique !== geracaoRequisicao || aliasAtual !== aliasEsperado) return;

                    if (resRev.ok) {
                        if (timerCardSenha) clearTimeout(timerCardSenha);
                        spanInfoSenha.style.display = 'none';
                        spanValorRevelado.textContent = `🔑 Senha: ${resRev.data.senha}`;
                        spanValorRevelado.style.display = 'inline';
                        mostrarAlerta('Senha revelada temporariamente por 20 segundos (auditado).', 'sucesso');

                        timerCardSenha = setTimeout(() => {
                            spanValorRevelado.textContent = '';
                            spanValorRevelado.style.display = 'none';
                            spanInfoSenha.style.display = 'inline';
                        }, 20000);
                    } else {
                        mostrarAlerta(resRev.erro || 'Falha ao revelar senha.', 'erro');
                    }
                });
                acoesSenha.appendChild(btnRevelar);

                const btnCopiar = document.createElement('button');
                btnCopiar.type = 'button';
                btnCopiar.className = 'btn btn-sm';
                btnCopiar.textContent = '📋 Copiar';
                btnCopiar.addEventListener('click', async () => {
                    const genClique = geracaoRequisicao;
                    const idEsperado = rotina.id;
                    const aliasEsperado = aliasAtual;
                    const resCop = await apiFetch(`/admin/backups-ftp/rotina/${idEsperado}/copiar-senha`, { method: 'POST' });

                    if (genClique !== geracaoRequisicao || aliasAtual !== aliasEsperado) return;

                    if (resCop.ok) {
                        copiarTextoClipboard(resCop.data.senha, 'Senha copiada para o clipboard! (Ação auditada no banco)');
                    } else {
                        mostrarAlerta(resCop.erro || 'Falha ao copiar senha.', 'erro');
                    }
                });
                acoesSenha.appendChild(btnCopiar);

                blocoSenha.appendChild(acoesSenha);
            } else {
                spanInfoSenha.textContent = '🔑 Senha: Nenhuma cadastrada';
                blocoSenha.appendChild(spanInfoSenha);
            }
            card.appendChild(blocoSenha);

            // Bloco de Notas / Observações
            if (rotina.notas) {
                const divNotas = document.createElement('div');
                divNotas.style.fontSize = '0.82em';
                divNotas.style.color = 'var(--cor-texto-suave, #64748b)';
                divNotas.style.marginTop = '6px';
                const strong = document.createElement('strong');
                strong.textContent = 'Notas: ';
                divNotas.appendChild(strong);
                divNotas.appendChild(document.createTextNode(rotina.notas));
                card.appendChild(divNotas);
            }

            // Bloco de Cobian Lists
            const rodapeCard = document.createElement('div');
            rodapeCard.className = 'ftp-rotina-card-rodape';

            const infoList = document.createElement('span');
            infoList.className = 'info-listas';
            if (rotina.total_listas > 0 && rotina.ultima_lista) {
                infoList.textContent = `📄 Cobian: v${rotina.ultima_lista.versao_numero} (${rotina.ultima_lista.tamanho_bytes} B) - ${rotina.ultima_lista.enviado_em ? rotina.ultima_lista.enviado_em.substring(0, 10) : ''}`;
            } else {
                infoList.textContent = '📄 Nenhuma lista Cobian enviada';
            }
            rodapeCard.appendChild(infoList);

            const btnCobian = document.createElement('button');
            btnCobian.type = 'button';
            btnCobian.className = 'btn btn-sm btn-disparo';
            btnCobian.textContent = '📁 Gerenciar Listas Cobian';
            btnCobian.addEventListener('click', () => abrirVisaoListasCobian(rotina));
            rodapeCard.appendChild(btnCobian);

            card.appendChild(rodapeCard);
            containerCardsRotinas.appendChild(card);
        });
    }

    async function excluirRotina(rotina) {
        if (!confirm(`Tem certeza que deseja excluir a rotina "${rotina.nome_rotina}" e todas as suas listas versionadas?\nEsta ação será auditada e é irreversível.`)) {
            return;
        }

        limparAlerta();
        const res = await apiFetch(`/admin/backups-ftp/rotina/${rotina.id}`, { method: 'DELETE' });
        if (res.ok) {
            mostrarAlerta('Rotina excluída com sucesso.', 'sucesso');
            carregarRotinasCliente(aliasAtual);
        } else {
            mostrarAlerta(res.erro || 'Falha ao excluir rotina.', 'erro');
        }
    }

    // -------------------------------------------------------------------------
    // Visão 2: Formulário de Rotina (Criar e Editar)
    // -------------------------------------------------------------------------

    function criarLinhaCampoExtra(chave = '', valorInicialOuPlaceholder = '', chaveExistente = false, ehSensivel = false) {
        const linha = document.createElement('div');
        linha.className = 'ftp-linha-campo-extra';

        const inputChave = document.createElement('input');
        inputChave.type = 'text';
        inputChave.className = 'ftp-input extra-chave';
        inputChave.placeholder = 'Nome do campo (ex: pasta_destino)';
        inputChave.value = chave;
        if (chaveExistente) {
            inputChave.dataset.chaveOriginal = chave;
            inputChave.dataset.eraSensivelOriginal = ehSensivel ? '1' : '0';
            inputChave.readOnly = true; // Chaves já salvas são estritamente read-only
        }
        linha.appendChild(inputChave);

        // Input de valor: type="password" se sensível, type="text" se não-sensível
        const inputValor = document.createElement('input');
        inputValor.type = ehSensivel ? 'password' : 'text';
        inputValor.className = 'ftp-input extra-valor';
        if (ehSensivel) {
            inputValor.placeholder = chaveExistente ? '•••••••• (em branco mantém valor)' : (valorInicialOuPlaceholder || 'Valor confidencial');
            inputValor.value = chaveExistente ? '' : (valorInicialOuPlaceholder || '');
        } else {
            inputValor.placeholder = 'Valor do campo';
            inputValor.value = valorInicialOuPlaceholder || '';
        }
        linha.appendChild(inputValor);

        // Checkbox "Confidencial"
        const labelSensivel = document.createElement('label');
        labelSensivel.className = 'ftp-label-sensivel';
        labelSensivel.title = 'Todos os extras são sempre cifrados no banco; este checkbox apenas oculta o valor na interface e exige revelação auditada.';

        const checkboxSensivel = document.createElement('input');
        checkboxSensivel.type = 'checkbox';
        checkboxSensivel.className = 'extra-sensivel';
        checkboxSensivel.checked = !!ehSensivel;
        labelSensivel.appendChild(checkboxSensivel);

        const spanTextoLabel = document.createElement('span');
        spanTextoLabel.textContent = '🔒 Confidencial';
        labelSensivel.appendChild(spanTextoLabel);
        linha.appendChild(labelSensivel);

        // Span sibling read-only para exibição temporária de 20s do valor revelado (NUNCA auto-cola no input)
        const spanValorReveladoExtra = document.createElement('span');
        spanValorReveladoExtra.className = 'extra-valor-revelado';
        spanValorReveladoExtra.style.display = 'none';
        spanValorReveladoExtra.style.fontFamily = 'monospace';
        spanValorReveladoExtra.style.fontSize = '0.82em';
        spanValorReveladoExtra.style.color = 'var(--cor-primaria, #2563eb)';
        spanValorReveladoExtra.style.padding = '2px 6px';
        spanValorReveladoExtra.style.background = '#eff6ff';
        spanValorReveladoExtra.style.borderRadius = '4px';
        spanValorReveladoExtra.style.userSelect = 'all';
        linha.appendChild(spanValorReveladoExtra);

        let btnRevExtra = null;
        let btnCopExtra = null;
        let timerExtra = null;

        if (chaveExistente && rotinaAtual) {
            btnRevExtra = document.createElement('button');
            btnRevExtra.type = 'button';
            btnRevExtra.className = 'btn btn-sm btn-revelar-extra';
            btnRevExtra.title = 'Revelar valor do campo extra por 20s (auditado)';
            btnRevExtra.textContent = '👁';
            btnRevExtra.style.display = ehSensivel ? 'inline-block' : 'none';
            btnRevExtra.addEventListener('click', async () => {
                const genClique = geracaoRequisicao;
                const idEsperado = rotinaAtual ? rotinaAtual.id : null;
                const resRev = await apiFetch(`/admin/backups-ftp/rotina/${idEsperado}/revelar-extra`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ chave })
                });

                // Descarta se a tela mudou, rotina desassociada, checkbox desmarcado ou linha removida do DOM
                if (genClique !== geracaoRequisicao || !rotinaAtual || rotinaAtual.id !== idEsperado || !checkboxSensivel.checked || !linha.isConnected) return;

                if (resRev.ok) {
                    if (timerExtra) clearTimeout(timerExtra);
                    // NUNCA auto-cola no inputValor: exibe exclusivamente no span read-only
                    spanValorReveladoExtra.textContent = `${chave}: ${resRev.data.valor}`;
                    spanValorReveladoExtra.style.display = 'inline-block';
                    mostrarAlerta(`Valor do campo extra "${chave}" revelado por 20 segundos (auditado).`, 'sucesso');

                    timerExtra = setTimeout(() => {
                        spanValorReveladoExtra.textContent = '';
                        spanValorReveladoExtra.style.display = 'none';
                    }, 20000);
                } else {
                    mostrarAlerta(resRev.erro || 'Falha ao revelar campo extra.', 'erro');
                }
            });
            linha.appendChild(btnRevExtra);

            btnCopExtra = document.createElement('button');
            btnCopExtra.type = 'button';
            btnCopExtra.className = 'btn btn-sm btn-copiar-extra';
            btnCopExtra.title = 'Copiar valor do campo extra (auditado)';
            btnCopExtra.textContent = '📋';
            btnCopExtra.style.display = ehSensivel ? 'inline-block' : 'none';
            btnCopExtra.addEventListener('click', async () => {
                const genClique = geracaoRequisicao;
                const idEsperado = rotinaAtual ? rotinaAtual.id : null;
                const resCop = await apiFetch(`/admin/backups-ftp/rotina/${idEsperado}/copiar-extra`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ chave })
                });

                // Descarta se a tela mudou, rotina desassociada, checkbox desmarcado ou linha removida do DOM
                if (genClique !== geracaoRequisicao || !rotinaAtual || rotinaAtual.id !== idEsperado || !checkboxSensivel.checked || !linha.isConnected) return;

                if (resCop.ok) {
                    copiarTextoClipboard(resCop.data.valor, `Campo "${chave}" copiado! (Ação auditada)`);
                } else {
                    mostrarAlerta(resCop.erro || 'Falha ao copiar campo extra.', 'erro');
                }
            });
            linha.appendChild(btnCopExtra);
        }

        // Listener do Toggle do Checkbox Confidencial
        checkboxSensivel.addEventListener('change', () => {
            const sensivelAgora = checkboxSensivel.checked;
            if (sensivelAgora) {
                // Mudou para confidencial: mascara input com password
                inputValor.type = 'password';
                if (chaveExistente) {
                    if (!inputValor.value) {
                        inputValor.placeholder = '•••••••• (em branco mantém valor)';
                    }
                    if (btnRevExtra) btnRevExtra.style.display = 'inline-block';
                    if (btnCopExtra) btnCopExtra.style.display = 'inline-block';
                } else {
                    inputValor.placeholder = 'Valor confidencial';
                }
            } else {
                // Mudou para não-confidencial: input text visível
                inputValor.type = 'text';
                if (btnRevExtra) btnRevExtra.style.display = 'none';
                if (btnCopExtra) btnCopExtra.style.display = 'none';
                if (spanValorReveladoExtra) {
                    spanValorReveladoExtra.style.display = 'none';
                    spanValorReveladoExtra.textContent = '';
                }
                if (timerExtra) {
                    clearTimeout(timerExtra);
                    timerExtra = null;
                }
                if (chaveExistente) {
                    // Se o campo era confidencial no banco e o input está em branco, NÃO buscar segredo sem auditoria!
                    // O valor pré-existente será preservado (enviando null) e decifrado/convertido pelo backend ao salvar.
                    if (!inputValor.value) {
                        inputValor.placeholder = '(mantém valor ao salvar; ou digite novo)';
                    }
                } else {
                    inputValor.placeholder = 'Valor do campo';
                }
            }
        });

        const btnRemoverLinha = document.createElement('button');
        btnRemoverLinha.type = 'button';
        btnRemoverLinha.className = 'btn btn-sm btn-perigo';
        btnRemoverLinha.textContent = '✕';
        btnRemoverLinha.title = 'Remover este campo';
        btnRemoverLinha.addEventListener('click', () => {
            if (timerExtra) clearTimeout(timerExtra);
            linha.remove();
        });
        linha.appendChild(btnRemoverLinha);

        containerExtras.appendChild(linha);
    }

    function abrirFormNovaRotina() {
        // Invalida requisições pendentes de rotinas anteriores (evita vazamento de senha/extra)
        geracaoRequisicao++;

        rotinaAtual = null;
        chavesExtrasOriginais = [];
        limparAlerta();
        limparTimerSenhaForm();

        formTitulo.textContent = `Nova Rotina — ${aliasAtual}`;
        campoRotinaId.value = '';
        campoRevision.value = '0';

        campoNome.value = '';
        campoServidor.value = '';
        campoPorta.value = '21';
        campoUsuario.value = '';
        campoSenha.value = '';
        campoSenha.placeholder = 'Digite a senha da rotina';
        btnRevelarSenhaForm.style.display = 'none';
        btnCopiarSenhaForm.style.display = 'none';
        lblRemoverSenha.style.display = 'none';
        chkRemoverSenha.checked = false;
        senhaStatusInfo.textContent = 'Defina a senha caso o acesso FTP exija credenciais.';

        campoResponsavel.value = '';
        campoLancamento.value = '';
        campoNotas.value = '';
        containerExtras.textContent = '';

        visaoRotinas.style.display = 'none';
        visaoListas.style.display = 'none';
        visaoFormRotina.style.display = 'block';
    }

    function abrirFormEditarRotina(rotina) {
        // Invalida requisições pendentes anteriores
        geracaoRequisicao++;

        rotinaAtual = rotina;
        limparAlerta();
        limparTimerSenhaForm();

        formTitulo.textContent = `Editar Rotina: ${rotina.nome_rotina}`;
        campoRotinaId.value = rotina.id;
        campoRevision.value = rotina.revision || '1';

        campoNome.value = rotina.nome_rotina || '';
        campoServidor.value = rotina.servidor || '';
        campoPorta.value = rotina.porta || '21';
        campoUsuario.value = rotina.usuario || '';
        campoResponsavel.value = rotina.responsavel || '';
        campoLancamento.value = rotina.lancamento_em || '';
        campoNotas.value = rotina.notas || '';

        // Tratamento da Senha: o input de nova senha fica sempre vazio
        campoSenha.value = '';
        chkRemoverSenha.checked = false;

        if (rotina.senha_configurada) {
            campoSenha.placeholder = 'Deixe em branco para manter a senha atual';
            btnRevelarSenhaForm.style.display = 'inline-block';
            btnCopiarSenhaForm.style.display = 'inline-block';
            lblRemoverSenha.style.display = 'inline-block';
            senhaStatusInfo.textContent = 'Senha cadastrada e protegida no banco. Preencha apenas se desejar alterá-la.';
        } else {
            campoSenha.placeholder = 'Nenhuma senha cadastrada. Digite para definir';
            btnRevelarSenhaForm.style.display = 'none';
            btnCopiarSenhaForm.style.display = 'none';
            lblRemoverSenha.style.display = 'none';
            senhaStatusInfo.textContent = 'Nenhuma senha configurada para esta rotina.';
        }

        // Campos Extras
        containerExtras.textContent = '';
        chavesExtrasOriginais = Array.isArray(rotina.extras_configurados) ? [...rotina.extras_configurados] : [];
        const mapaSensiveis = rotina.extras_sensiveis || {};
        const dadosExtrasObj = rotina.dados_extras || {};

        chavesExtrasOriginais.forEach(ch => {
            // Se constar no mapa extras_sensiveis, usa o booleano; se for legado (ausente), default é true
            const ehSensivel = (typeof mapaSensiveis[ch] === 'boolean') ? mapaSensiveis[ch] : true;
            if (ehSensivel) {
                criarLinhaCampoExtra(ch, '•••••••• (em branco mantém valor)', true, true);
            } else {
                const valorPlano = (dadosExtrasObj[ch] != null) ? String(dadosExtrasObj[ch]) : '';
                criarLinhaCampoExtra(ch, valorPlano, true, false);
            }
        });

        visaoRotinas.style.display = 'none';
        visaoListas.style.display = 'none';
        visaoFormRotina.style.display = 'block';
    }

    async function salvarRotina() {
        limparAlerta();
        const nome = (campoNome.value || '').trim();
        if (!nome) {
            mostrarAlerta('O nome da rotina é obrigatório.', 'erro');
            return;
        }

        const id = campoRotinaId.value;
        const revision = parseInt(campoRevision.value, 10) || 1;
        const servidor = (campoServidor.value || '').trim();
        const porta = (campoPorta.value || '').trim();
        const usuario = (campoUsuario.value || '').trim();
        const responsavel = (campoResponsavel.value || '').trim();
        const lancamento = (campoLancamento.value || '').trim();
        const notas = (campoNotas.value || '').trim();
        const senhaDigitada = campoSenha.value; // Preserva espaços exatos da senha

        // Coleta de extras
        const extrasPayload = {};
        const extrasSensiveisPayload = {};
        const chavesPresentesNoForm = [];
        const linhasExtras = containerExtras.querySelectorAll('.ftp-linha-campo-extra');

        linhasExtras.forEach(lin => {
            const inChave = lin.querySelector('.extra-chave');
            const inValor = lin.querySelector('.extra-valor');
            const chkSensivel = lin.querySelector('.extra-sensivel');
            const chave = (inChave ? inChave.value : '').trim();
            if (chave) {
                chavesPresentesNoForm.push(chave);
                const valor = inValor ? inValor.value : '';
                const ehSensivel = chkSensivel ? Boolean(chkSensivel.checked) : false;

                extrasSensiveisPayload[chave] = ehSensivel;

                const eraExistente = Boolean(inChave && inChave.dataset.chaveOriginal);
                const eraSensivelOriginal = Boolean(inChave && inChave.dataset.eraSensivelOriginal === '1');

                if (eraExistente && !valor && eraSensivelOriginal) {
                    extrasPayload[chave] = null;
                } else {
                    extrasPayload[chave] = valor;
                }
            }
        });

        // Identifica chaves que foram removidas pelo operador
        const removerExtras = chavesExtrasOriginais.filter(ch => !chavesPresentesNoForm.includes(ch));

        btnSalvarRotina.disabled = true;
        btnSalvarRotina.textContent = 'Salvando...';

        try {
            if (!id) {
                // Criação (POST)
                const payloadCriacao = {
                    cliente_alias: aliasAtual,
                    nome_rotina: nome,
                    servidor,
                    porta,
                    usuario,
                    senha: senhaDigitada || null,
                    responsavel,
                    lancamento_em: lancamento || null,
                    notas,
                    dados_extras: extrasPayload,
                    extras_sensiveis: extrasSensiveisPayload,
                    status: 1
                };

                const res = await apiFetch('/admin/backups-ftp/rotina', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payloadCriacao)
                });

                if (res.ok) {
                    mostrarAlerta('Rotina criada com sucesso!', 'sucesso');
                    geracaoRequisicao++; // Invalida pendências
                    limparTimerSenhaForm();
                    visaoFormRotina.style.display = 'none';
                    visaoRotinas.style.display = 'block';
                    carregarRotinasCliente(aliasAtual);
                } else {
                    mostrarAlerta(res.erro || 'Falha ao criar rotina.', 'erro');
                }
            } else {
                // Atualização (PUT com controle de concorrência revision)
                const payloadAtualizacao = {
                    expected_revision: revision,
                    nome_rotina: nome,
                    servidor,
                    porta,
                    usuario,
                    responsavel,
                    lancamento_em: lancamento || null,
                    notas,
                    remover_senha: chkRemoverSenha.checked,
                    dados_extras: extrasPayload,
                    extras_sensiveis: extrasSensiveisPayload,
                    remover_extras: removerExtras
                };

                if (senhaDigitada) {
                    payloadAtualizacao.senha = senhaDigitada;
                }

                const res = await apiFetch(`/admin/backups-ftp/rotina/${id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payloadAtualizacao)
                });

                if (res.ok) {
                    mostrarAlerta('Rotina atualizada com sucesso!', 'sucesso');
                    geracaoRequisicao++; // Invalida pendências
                    limparTimerSenhaForm();
                    visaoFormRotina.style.display = 'none';
                    visaoRotinas.style.display = 'block';
                    carregarRotinasCliente(aliasAtual);
                } else if (res.status === 409) {
                    mostrarAlerta('Conflito de concorrência: a rotina foi modificada por outro operador. Por favor, cancele e reabra os dados.', 'erro');
                } else {
                    mostrarAlerta(res.erro || 'Falha ao atualizar rotina.', 'erro');
                }
            }
        } finally {
            btnSalvarRotina.disabled = false;
            btnSalvarRotina.textContent = 'Salvar Rotina';
        }
    }

    // -------------------------------------------------------------------------
    // Visão 3: Painel e Versionamento de Listas Cobian (.list / .lst)
    // -------------------------------------------------------------------------

    async function abrirVisaoListasCobian(rotina) {
        geracaoRequisicao++; // Invalida requisições pendentes da visão anterior
        rotinaAtual = rotina;
        limparAlerta();
        limparTimerSenhaForm();

        visaoRotinas.style.display = 'none';
        visaoFormRotina.style.display = 'none';
        visaoListas.style.display = 'block';

        if (inputArquivoList) inputArquivoList.value = '';
        if (inputComentarioList) inputComentarioList.value = '';

        carregarVersoesListas(rotina.id);
    }

    async function carregarVersoesListas(rotinaId) {
        const genAtual = geracaoRequisicao;
        containerTabelaVersoes.textContent = 'Carregando histórico de listas Cobian...';

        const res = await apiFetch(`/admin/backups-ftp/rotina/${rotinaId}/listas`);
        if (genAtual !== geracaoRequisicao || !rotinaAtual || rotinaAtual.id !== rotinaId) return;

        containerTabelaVersoes.textContent = '';

        if (!res.ok) {
            mostrarAlerta(res.erro || 'Falha ao carregar versões da lista.', 'erro');
            return;
        }

        const versoes = (res.data && res.data.versoes) ? res.data.versoes : [];
        if (versoes.length === 0) {
            const p = document.createElement('p');
            p.style.color = 'var(--cor-texto-suave, #64748b)';
            p.style.fontSize = '0.88em';
            p.textContent = 'Nenhuma versão de lista Cobian enviada para esta rotina ainda.';
            containerTabelaVersoes.appendChild(p);
            return;
        }

        const tabela = document.createElement('table');
        tabela.className = 'ftp-tabela-versoes';

        const thead = document.createElement('thead');
        const trH = document.createElement('tr');
        ['Versão', 'Tamanho', 'Encoding', 'SHA-256', 'Enviado Por', 'Data / Hora', 'Ações'].forEach(thNome => {
            const th = document.createElement('th');
            th.textContent = thNome;
            trH.appendChild(th);
        });
        thead.appendChild(trH);
        tabela.appendChild(thead);

        const tbody = document.createElement('tbody');
        versoes.forEach(v => {
            const tr = document.createElement('tr');

            const tdV = document.createElement('td');
            tdV.style.fontWeight = '700';
            tdV.textContent = `v${v.versao_numero}`;
            tr.appendChild(tdV);

            const tdTam = document.createElement('td');
            tdTam.textContent = `${v.tamanho_bytes} B`;
            tr.appendChild(tdTam);

            const tdEnc = document.createElement('td');
            tdEnc.textContent = v.encoding_detectado || '—';
            tr.appendChild(tdEnc);

            const tdHash = document.createElement('td');
            const codeHash = document.createElement('code');
            codeHash.textContent = (v.checksum_sha256 || '').substring(0, 12) + '…';
            codeHash.title = v.checksum_sha256;
            tdHash.appendChild(codeHash);
            tr.appendChild(tdHash);

            const tdPor = document.createElement('td');
            tdPor.textContent = v.enviado_por || '—';
            tr.appendChild(tdPor);

            const tdData = document.createElement('td');
            tdData.textContent = v.enviado_em ? v.enviado_em.replace('T', ' ').substring(0, 19) : '—';
            tr.appendChild(tdData);

            const tdAcao = document.createElement('td');
            const btnVer = document.createElement('button');
            btnVer.type = 'button';
            btnVer.className = 'btn btn-sm';
            btnVer.textContent = '👁 Inspecionar';
            btnVer.title = 'Inspecionar conteúdo da lista (auditado fail-closed)';
            btnVer.addEventListener('click', () => inspecionarConteudoLista(rotinaId, v.id, v.versao_numero));
            tdAcao.appendChild(btnVer);
            tr.appendChild(tdAcao);

            tbody.appendChild(tr);
        });
        tabela.appendChild(tbody);
        containerTabelaVersoes.appendChild(tabela);
    }

    async function enviarArquivoLista() {
        limparAlerta();
        if (!rotinaAtual) return;

        const files = inputArquivoList.files;
        if (!files || files.length === 0) {
            mostrarAlerta('Selecione um arquivo .list ou .lst para enviar.', 'erro');
            return;
        }

        const file = files[0];
        // Validação preventiva no cliente: máx 2 MiB (2097152 bytes)
        if (file.size > 2 * 1024 * 1024) {
            mostrarAlerta('Arquivo excede o limite máximo permitido de 2 MiB.', 'erro');
            return;
        }

        const formData = new FormData();
        formData.append('arquivo', file);
        formData.append('comentario', (inputComentarioList.value || '').trim());
        formData.append('csrf_token', obterCsrf());

        btnEnviarArquivoList.disabled = true;
        btnEnviarArquivoList.textContent = 'Enviando...';

        try {
            const res = await apiFetch(`/admin/backups-ftp/rotina/${rotinaAtual.id}/upload-list`, {
                method: 'POST',
                body: formData
            });

            if (res.ok) {
                mostrarAlerta('Lista Cobian enviada e versionada com sucesso!', 'sucesso');
                inputArquivoList.value = '';
                inputComentarioList.value = '';
                carregarVersoesListas(rotinaAtual.id);
            } else {
                mostrarAlerta(res.erro || 'Falha ao enviar lista Cobian.', 'erro');
            }
        } finally {
            btnEnviarArquivoList.disabled = false;
            btnEnviarArquivoList.textContent = 'Upload e Versionar';
        }
    }

    async function inspecionarConteudoLista(rotinaId, versaoId, versaoNumero) {
        limparAlerta();
        const res = await apiFetch(`/admin/backups-ftp/rotina/${rotinaId}/lista/${versaoId}/ver-conteudo`, {
            method: 'POST'
        });

        if (res.ok) {
            submodalTitulo.textContent = `Conteúdo da Lista Cobian — Versão ${versaoNumero}`;
            // Sanitização estrita: NUNCA usar innerHTML com conteúdo de lista
            preConteudoList.textContent = res.data.conteudo;
            submodalConteudo.style.display = 'flex';
        } else {
            mostrarAlerta(res.erro || 'Falha ao obter conteúdo da lista.', 'erro');
        }
    }

    // -------------------------------------------------------------------------
    // Eventos e Inicialização
    // -------------------------------------------------------------------------

    // Abertura do modal via clique nos botões dos cards de clientes
    document.addEventListener('click', (e) => {
        const btn = e.target.closest('.btn-gerenciar-ftp');
        if (btn) {
            const alias = btn.getAttribute('data-alias');
            if (alias) {
                abrirModalAdmin(alias);
            } else {
                abrirModalAdmin('');
            }
        }
    });

    // Seletor de Clientes no Topo do Modal
    if (selectCliente) {
        selectCliente.addEventListener('change', (e) => {
            const novoAlias = e.target.value;
            if (novoAlias && novoAlias !== aliasAtual) {
                abrirModalAdmin(novoAlias);
            }
        });
    }

    // Cadastrar rotina para outro cliente digitando alias manualmente
    if (btnNovoClienteManual) {
        btnNovoClienteManual.addEventListener('click', () => {
            const novo = prompt('Digite o alias do cliente para gerenciar rotinas:');
            if (novo && novo.trim()) {
                abrirModalAdmin(novo.trim());
            }
        });
    }

    // Fechar modais
    if (btnFecharModal) btnFecharModal.addEventListener('click', fecharModalAdmin);
    if (btnFecharSubmodal) btnFecharSubmodal.addEventListener('click', fecharSubmodalList);
    if (btnFecharSubmodalRodape) btnFecharSubmodalRodape.addEventListener('click', fecharSubmodalList);

    // Fechar com ESC
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            if (submodalConteudo && submodalConteudo.style.display === 'flex') {
                fecharSubmodalList();
            } else if (modal && modal.style.display === 'flex') {
                fecharModalAdmin();
            }
        }
    });

    // Visão 1 -> Visão 2
    if (btnCriarNovaRotina) btnCriarNovaRotina.addEventListener('click', abrirFormNovaRotina);

    // Visão 2 Form Ações
    if (btnAddCampoExtra) btnAddCampoExtra.addEventListener('click', () => criarLinhaCampoExtra());
    if (btnCancelarForm) {
        btnCancelarForm.addEventListener('click', () => {
            geracaoRequisicao++; // Invalida pendências assíncronas do form cancelado
            limparAlerta();
            limparTimerSenhaForm();
            visaoFormRotina.style.display = 'none';
            visaoRotinas.style.display = 'block';
        });
    }
    if (btnSalvarRotina) btnSalvarRotina.addEventListener('click', salvarRotina);

    // Revelar / Copiar no Form (NUNCA altera o input de nova senha)
    if (btnRevelarSenhaForm) {
        btnRevelarSenhaForm.addEventListener('click', async () => {
            if (!rotinaAtual) return;
            const genClique = geracaoRequisicao;
            const idEsperado = rotinaAtual.id;
            const resRev = await apiFetch(`/admin/backups-ftp/rotina/${idEsperado}/revelar-senha`, { method: 'POST' });

            // Descarta se o form foi cancelado, trocado ou salvo antes da response
            if (genClique !== geracaoRequisicao || !rotinaAtual || rotinaAtual.id !== idEsperado) return;

            if (resRev.ok) {
                limparTimerSenhaForm();
                // Exibe no span read-only dedicado, NÃO preenche o campoSenha editável
                senhaReveladaTexto.textContent = resRev.data.senha;
                senhaReveladaBox.style.display = 'block';
                senhaStatusInfo.textContent = 'Senha atual revelada por 20 segundos (auditado no banco).';
                mostrarAlerta('Senha atual revelada temporariamente (auditado).', 'sucesso');

                timerMascaraSenhaForm = setTimeout(() => {
                    limparTimerSenhaForm();
                    senhaStatusInfo.textContent = 'Máscara reativada automaticamente (20s).';
                }, 20000);
            } else {
                mostrarAlerta(resRev.erro || 'Falha ao revelar senha.', 'erro');
            }
        });
    }

    if (btnCopiarSenhaForm) {
        btnCopiarSenhaForm.addEventListener('click', async () => {
            if (!rotinaAtual) return;
            const genClique = geracaoRequisicao;
            const idEsperado = rotinaAtual.id;
            const resCop = await apiFetch(`/admin/backups-ftp/rotina/${idEsperado}/copiar-senha`, { method: 'POST' });

            if (genClique !== geracaoRequisicao || !rotinaAtual || rotinaAtual.id !== idEsperado) return;

            if (resCop.ok) {
                copiarTextoClipboard(resCop.data.senha, 'Senha copiada para o clipboard! (Ação auditada)');
            } else {
                mostrarAlerta(resCop.erro || 'Falha ao copiar senha.', 'erro');
            }
        });
    }

    // Visão 3 Listas Cobian Ações
    if (btnFecharVisaoListas) {
        btnFecharVisaoListas.addEventListener('click', () => {
            geracaoRequisicao++; // Invalida pendências assíncronas de listas
            limparTimerSenhaForm();
            visaoListas.style.display = 'none';
            visaoRotinas.style.display = 'block';
            carregarRotinasCliente(aliasAtual);
        });
    }
    if (btnEnviarArquivoList) btnEnviarArquivoList.addEventListener('click', enviarArquivoLista);

})();
