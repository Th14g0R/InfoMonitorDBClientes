window.avisarSemPermissao = function(area) {
    window.alert(`Você não tem permissão para acessar ${area}. Fale com o administrador Master.`);
    return false;
};

document.addEventListener('DOMContentLoaded', () => {
    // ===== TEMA (Light/Dark) =====
    const THEME_KEY = 'infomonitor-theme';
    const html = document.documentElement;
    const toggleBtn = document.getElementById('theme-toggle');

    // Aplica tema salvo ou detecta preferência do sistema
    function applyTheme(theme) {
        html.setAttribute('data-theme', theme);
        localStorage.setItem(THEME_KEY, theme);
        updateToggleIcon(theme);
    }

    function updateToggleIcon(theme) {
        if (toggleBtn) {
            toggleBtn.setAttribute('aria-label', theme === 'dark' ? 'Alternar para tema claro' : 'Alternar para tema escuro');
        }
    }

    // Inicializa tema
    const savedTheme = localStorage.getItem(THEME_KEY);
    if (savedTheme) {
        applyTheme(savedTheme);
    } else if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
        applyTheme('dark');
    } else {
        applyTheme('light');
    }

    // Toggle manual
    if (toggleBtn) {
        toggleBtn.addEventListener('click', () => {
            const current = html.getAttribute('data-theme') || 'light';
            applyTheme(current === 'dark' ? 'light' : 'dark');
        });
    }

    // Escuta mudança de preferência do sistema (se usuário não definiu preferência manual)
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
        if (!localStorage.getItem(THEME_KEY)) {
            applyTheme(e.matches ? 'dark' : 'light');
        }
    });

    // ===== SKIP LINK (Acessibilidade) =====
    // Garante que o skip link funcione corretamente
    const skipLink = document.querySelector('.skip-link');
    if (skipLink) {
        skipLink.addEventListener('click', (e) => {
            const targetId = skipLink.getAttribute('href');
            if (targetId) {
                const target = document.querySelector(targetId);
                if (target) {
                    e.preventDefault();
                    target.focus();
                    target.scrollIntoView({ behavior: 'smooth' });
                }
            }
        });
    }

    // ===== UPLOAD DE ARQUIVOS =====
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

    // ===== MENUS DROPDOWN =====
    const menus = document.querySelectorAll('.menu-acoes');
    document.addEventListener('click', event => {
        menus.forEach(menu => { if (!menu.contains(event.target)) menu.open = false; });
    });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') menus.forEach(menu => {
            if (menu.open) { menu.open = false; menu.querySelector('summary').focus(); }
        });
    });

    // ===== NAVEGAÇÃO DE SERVIDORES =====
    document.getElementById('servidor-navegacao')?.addEventListener('change', event => {
        window.location.assign(event.target.value);
    });

    // ===== FOCUS TRAP PARA MODAIS (Acessibilidade) =====
    let focusTrapStack = [];
    let lastFocusedElement = null;

    function trapFocus(modal) {
        const focusableElements = modal.querySelectorAll(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        const firstElement = focusableElements[0];
        const lastElement = focusableElements[focusableElements.length - 1];

        function handleTab(e) {
            if (e.key !== 'Tab') return;

            if (e.shiftKey) {
                if (document.activeElement === firstElement) {
                    e.preventDefault();
                    lastElement.focus();
                }
            } else {
                if (document.activeElement === lastElement) {
                    e.preventDefault();
                    firstElement.focus();
                }
            }
        }

        modal.addEventListener('keydown', handleTab);
        modal._focusTrapHandler = handleTab;

        // Foca no primeiro elemento
        if (firstElement) firstElement.focus();
    }

    function releaseFocusTrap(modal) {
        if (modal._focusTrapHandler) {
            modal.removeEventListener('keydown', modal._focusTrapHandler);
            modal._focusTrapHandler = null;
        }
    }

    window.openModal = function(modalId) {
        const modal = document.getElementById(modalId);
        const overlay = document.querySelector('.focus-trap-overlay');
        if (!modal) return;

        lastFocusedElement = document.activeElement;
        modal.classList.add('active');
        modal.setAttribute('aria-hidden', 'false');
        if (overlay) overlay.classList.add('active');
        document.body.style.overflow = 'hidden';
        focusTrapStack.push(modal);
        trapFocus(modal);
    };

    window.closeModal = function(modalId) {
        const modal = document.getElementById(modalId);
        const overlay = document.querySelector('.focus-trap-overlay');
        if (!modal) return;

        modal.classList.remove('active');
        modal.setAttribute('aria-hidden', 'true');
        if (overlay) overlay.classList.remove('active');
        document.body.style.overflow = '';
        releaseFocusTrap(modal);
        focusTrapStack = focusTrapStack.filter(m => m !== modal);

        // Restaura foco no elemento anterior
        if (lastFocusedElement) {
            lastFocusedElement.focus();
            lastFocusedElement = null;
        }
    };

    // Fecha modal ao clicar no overlay
    document.querySelector('.focus-trap-overlay')?.addEventListener('click', () => {
        const activeModal = focusTrapStack[focusTrapStack.length - 1];
        if (activeModal) {
            window.closeModal(activeModal.id);
        }
    });

    // Fecha modal com Escape
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && focusTrapStack.length > 0) {
            const activeModal = focusTrapStack[focusTrapStack.length - 1];
            window.closeModal(activeModal.id);
        }
    });

    // Botões de fechar modal
    document.querySelectorAll('.modal-close').forEach(btn => {
        btn.addEventListener('click', () => {
            const modal = btn.closest('.modal');
            if (modal) window.closeModal(modal.id);
        });
    });

    // ===== TABELAS RESPONSIVAS (cards no mobile) =====
    function convertTablesToCards() {
        const tables = document.querySelectorAll('table.table-cards');
        const isMobile = window.innerWidth <= 700;

        tables.forEach(table => {
            if (isMobile) {
                // Adiciona data-label às células baseado no cabeçalho
                const headers = Array.from(table.querySelectorAll('th')).map(th => th.textContent.trim());
                table.querySelectorAll('tbody tr').forEach(row => {
                    row.querySelectorAll('td').forEach((cell, index) => {
                        if (headers[index]) {
                            cell.setAttribute('data-label', headers[index]);
                        }
                    });
                });
                table.classList.add('table-cards');
            } else {
                table.classList.remove('table-cards');
                table.querySelectorAll('td').forEach(cell => cell.removeAttribute('data-label'));
            }
        });
    }

    // Executa na carga e no resize
    convertTablesToCards();
    let resizeTimer;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(convertTablesToCards, 100);
    });

    // ===== LOADING STATE =====
    window.showLoading = function(element) {
        if (element) {
            element.classList.add('loading');
            element.setAttribute('aria-busy', 'true');
        }
    };

    window.hideLoading = function(element) {
        if (element) {
            element.classList.remove('loading');
            element.removeAttribute('aria-busy');
        }
    };

    // ===== EMPTY STATE =====
    window.showEmptyState = function(container, message, iconSvg = '') {
        if (!container) return;
        container.innerHTML = `
            <div class="empty-state">
                ${iconSvg || '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9.172 16.172a4 4 0 015.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>'}
                <h3>Nenhum resultado</h3>
                <p>${message}</p>
            </div>
        `;
    };

    // ===== TOASTS (notificações) =====
    window.showToast = function(message, type = 'info', duration = 5000) {
        let container = document.querySelector('.toast-container');
        if (!container) {
            container = document.createElement('div');
            container.className = 'toast-container';
            document.body.appendChild(container);
        }
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.innerHTML = `
            <span>${message}</span>
            <button class="toast-close" aria-label="Fechar">&times;</button>
        `;
        toast.querySelector('.toast-close').addEventListener('click', () => toast.remove());
        container.appendChild(toast);
        if (duration > 0) setTimeout(() => toast.remove(), duration);
    };

    // Converte flash messages em toasts (se houver)
    document.querySelectorAll('.alert-box').forEach(box => {
        const text = box.textContent.trim();
        const type = box.classList.contains('alert-success') ? 'success' :
                     box.classList.contains('alert-warning') ? 'warning' :
                     box.classList.contains('alert-danger') ? 'danger' : 'info';
        window.showToast(text, type);
        box.remove();
    });
});
