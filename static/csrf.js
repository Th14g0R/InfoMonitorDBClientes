function csrfFetch(input, options = {}) {
    const url = new URL(input instanceof Request ? input.url : input, window.location.href);
    const method = (options.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    if (url.origin === window.location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
        const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
        headers.set('X-CSRF-Token', document.querySelector('meta[name="csrf-token"]').content);
        options = {...options, headers};
    }
    return fetch(input, options);
}
