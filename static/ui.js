document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('input[type="file"]').forEach(input => {
        const controle = document.createElement('div');
        controle.className = 'arquivo-controle';
        const botao = document.createElement('span');
        botao.className = 'arquivo-botao';
        botao.textContent = 'Selecionar arquivo';
        const nome = document.createElement('span');
        nome.className = 'arquivo-nome';
        nome.setAttribute('aria-live', 'polite');
        const atualizar = () => {
            nome.textContent = input.files.length ? Array.from(input.files, file => file.name).join(', ') : 'Nenhum arquivo selecionado';
            input.title = nome.textContent;
        };
        input.before(controle);
        controle.append(botao, nome, input);
        input.addEventListener('change', atualizar);
        input.form?.addEventListener('reset', () => setTimeout(atualizar, 0));
        atualizar();
    });
    const menus = document.querySelectorAll('.menu-acoes');
    document.addEventListener('click', event => {
        menus.forEach(menu => { if (!menu.contains(event.target)) menu.open = false; });
    });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') menus.forEach(menu => {
            if (menu.open) { menu.open = false; menu.querySelector('summary').focus(); }
        });
    });
    document.getElementById('servidor-navegacao')?.addEventListener('change', event => {
        window.location.assign(event.target.value);
    });
});
