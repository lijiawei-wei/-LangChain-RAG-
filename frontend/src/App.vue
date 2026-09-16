<script setup>
/**
 * 根组件。
 *
 * 职责只有一个：根据当前网址渲染对应页面。
 * 具体布局（顶栏、侧边栏）在 layouts/MainLayout.vue 里，
 * 登录页/注册页不属于主布局，所以它们直接由这里渲染。
 */
</script>

<template>
  <router-view />
</template>
