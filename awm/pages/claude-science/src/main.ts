import { mount } from 'svelte';
// Carries the reset and the html/body background — without it the dark app is
// framed in the UA's white 8px margin. Same load-bearing import as every page.
import '@awm/primitives/tokens.css';
import App from './App.svelte';

const target = document.getElementById('app');
if (!target) throw new Error('claude-science: #app not found');

mount(App, { target });
