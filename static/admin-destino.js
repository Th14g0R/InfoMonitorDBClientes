function atualizarCaminhoPadrao() {
    const form = document.getElementById('dbForm');
    const alias = form.alias.value.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^a-z0-9]/g, '');
    const existente = form.modo_destino.value === 'existente';
    const base = form.servidor.value === 'DB05' ? '/opt/infobrasil/3.0' : '/opt/infobrasil';
    const pasta = existente ? form.pasta_destino.value : (alias ? `${base}/${alias}` : '');
    form.caminho.value = pasta ? `${pasta}/${form.nome_arquivo.value.trim()}` : '';
}

document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('dbForm');
    if (!form) return;
    const painel = document.getElementById('pastasExistentes');
    const status = document.getElementById('statusPastas');
    const abrir = document.getElementById('abrirPasta');
    const voltar = document.getElementById('voltarPasta');
    let pai = null;
    let atual = '';
    let carregando = false;
    async function carregar(caminho = '') {
        carregando = true;
        form.pasta_destino.disabled = true;
        abrir.disabled = voltar.disabled = true;
        status.textContent = 'Carregando pastas…';
        try {
            const params = new URLSearchParams({servidor: form.servidor.value, caminho});
            const response = await csrfFetch(`/admin/pastas-destino?${params}`);
            if (!response.ok) {
                const erro = await response.json().catch(() => ({}));
                throw new Error(erro.erro || 'Não foi possível listar as pastas.');
            }
            const dados = await response.json();
            atual = dados.atual;
            pai = dados.pai;
            form.pasta_destino.replaceChildren(new Option(`${atual} (esta pasta)`, atual));
            dados.pastas.forEach(pasta => form.pasta_destino.add(new Option(pasta.nome, pasta.caminho)));
            status.textContent = `${dados.pastas.length} subpasta(s) em ${atual}`;
        } catch (erro) {
            form.pasta_destino.replaceChildren(new Option('Não foi possível carregar as pastas', ''));
            status.textContent = erro.message;
        } finally {
            carregando = false;
            form.pasta_destino.disabled = form.modo_destino.value !== 'existente';
            abrir.disabled = !form.pasta_destino.value || form.pasta_destino.value === atual;
            voltar.disabled = !pai;
            atualizarCaminhoPadrao();
        }
    }
    form.modo_destino.addEventListener('change', () => {
        const existente = form.modo_destino.value === 'existente';
        painel.hidden = !existente;
        form.pasta_destino.required = existente;
        form.pasta_destino.disabled = !existente || carregando;
        if (existente && !form.pasta_destino.value) carregar();
        atualizarCaminhoPadrao();
    });
    form.pasta_destino.addEventListener('change', () => {
        abrir.disabled = !form.pasta_destino.value || form.pasta_destino.value === atual;
        atualizarCaminhoPadrao();
    });
    form.nome_arquivo.addEventListener('input', atualizarCaminhoPadrao);
    abrir.addEventListener('click', () => carregar(form.pasta_destino.value));
    voltar.addEventListener('click', () => carregar(pai));
    document.getElementById('recarregarPastas').addEventListener('click', () => carregar(atual));
    form.addEventListener('submit', event => {
        if (form.modo_destino.value === 'existente' && (carregando || !form.pasta_destino.value)) {
            event.preventDefault();
            status.textContent = 'Aguarde a listagem e selecione uma pasta antes de enviar.';
        }
    });
    atualizarCaminhoPadrao();
});
