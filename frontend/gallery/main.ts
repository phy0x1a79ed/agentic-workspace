import { mount } from 'svelte';
import Gallery from './Gallery.svelte';
import './style.css';

const target = document.getElementById('app');
if (!target) throw new Error('gallery: #app not found');

mount(Gallery, { target });
