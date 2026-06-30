import { mount } from 'svelte';
import App from './App.svelte';
import '@awm/primitives/style.css';
import '@awm/primitives/tokens.css';

const target = document.getElementById('app');
if (!target) throw new Error('settings: #app not found');

mount(App, { target });
