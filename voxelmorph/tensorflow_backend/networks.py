import numpy as np
import neuron as ne
import tensorflow as tf

from tensorflow import keras
import tensorflow
import tensorflow.keras.backend as K
import tensorflow.keras.layers as KL
from tensorflow.keras.models import Model, Sequential
from tensorflow.keras.layers import Layer, Conv3D, Activation, Input, UpSampling3D
from tensorflow.keras.layers import concatenate, LeakyReLU, Reshape, Lambda
from tensorflow.keras.initializers import RandomNormal, Constant

from .. import utils
from . import layers
from .model_io import LoadableModel, store_config_args


class VxmDense(LoadableModel):
    """
    VoxelMorph network for (unsupervised) nonlinear registration between two images.
    """

    @store_config_args
    def __init__(self, inshape, enc_nf, dec_nf, int_steps=7, int_downsize=2, bidir=False, use_probs=False, src_feats=1, trg_feats=1, input_model=None):
        """ 
        Parameters:
            inshape: Input shape. e.g. (192, 192, 192)
            enc_nf: List of encoder filters. e.g. [16, 32, 32, 32]
            dec_nf: List of decoder filters. e.g. [32, 32, 32, 32, 32, 16, 16]
            int_steps: Number of flow integration steps. The warp is non-diffeomorphic when this value is 0.
            int_downsize: Integer specifying the flow downsample factor for vector integration. The flow field
                is not downsampled when this value is 1.
            bidir: Enable bidirectional cost function. Default is False.
            use_probs: Use probabilities in flow field. Default is False.
            src_feats: Number of source image features. Default is 1.
            trg_feats: Number of target image features. Default is 1.
            input_model: Model to concat with unet input layer.
        """

        # ensure correct dimensionality
        ndims = len(inshape)
        assert ndims in [1, 2, 3], 'ndims should be one of 1, 2, or 3. found: %d' % ndims

        # build core unet model and grab inputs
        unet_model = unet(inshape, enc_nf, dec_nf, src_feats=src_feats, trg_feats=trg_feats, input_model=input_model)
        source, target = unet_model.inputs[:2]

        # transform unet output into a flow field
        Conv = getattr(KL, 'Conv%dD' % ndims)
        flow_mean = Conv(ndims, kernel_size=3, padding='same',
                    kernel_initializer=RandomNormal(mean=0.0, stddev=1e-5), name='flow')(unet_model.output)

        # optionally include probabilities
        if use_probs:
            # initialize the velocity variance very low, to start stable
            flow_logsigma = Conv(ndims, kernel_size=3, padding='same',
                            kernel_initializer=RandomNormal(mean=0.0, stddev=1e-10),
                            bias_initializer=Constant(value=-10),
                            name='log_sigma')(unet_model.output)
            flow_params = concatenate([flow_mean, flow_logsigma])
            flow = ne.layers.SampleNormalLogVar(name="z_sample")([flow_mean, flow_logsigma])
        else:
            flow_params = flow_mean
            flow = flow_mean

        # optionally resize for integration
        if int_steps > 0 and int_downsize > 1:
            flow = layers.RescaleTransform(1 / int_downsize, name='resize')(flow)

        # optionally negate flow for bidirectional model
        pos_flow = flow
        if bidir:
            neg_flow = ne.layers.Negate()(flow)

        # integrate to produce diffeomorphic warp (i.e. treat flow as a stationary velocity field)
        if int_steps > 0:
            pos_flow = ne.layers.VecInt(method='ss', name='flow-int', int_steps=int_steps)(pos_flow)
            if bidir:
                neg_flow = ne.layers.VecInt(method='ss', name='neg_flow-int', int_steps=int_steps)(neg_flow)

            # resize to final resolution
            if int_downsize > 1:
                pos_flow = layers.RescaleTransform(int_downsize, name='diffflow')(pos_flow)
                if bidir:
                    neg_flow = layers.RescaleTransform(int_downsize, name='neg_diffflow')(neg_flow)

        # warp image with flow field
        y_source = layers.SpatialTransformer(interp_method='linear', indexing='ij', name='transformer')([source, pos_flow])
        if bidir:
            y_target = layers.SpatialTransformer(interp_method='linear', indexing='ij', name='neg_transformer')([target, neg_flow])

        # initialize the keras model
        outputs = [y_source, y_target, flow_params] if bidir else [y_source, flow_params]
        super().__init__(name='vxm_dense', inputs=unet_model.inputs, outputs=outputs)

        # cache pointers to layers and tensors for future reference
        self.references = LoadableModel.ReferenceContainer()
        self.references.unet_model = unet_model
        self.references.y_source = y_source
        self.references.y_target = y_target if bidir else None
        self.references.pos_flow = pos_flow
        self.references.neg_flow = neg_flow if bidir else None

    def get_predictor_model(self):
        """
        Extracts a predictor model from the VxmDense that directly outputs the warped image and 
        final diffeomorphic warp field (instead of the non-integrated flow field used for training).
        """
        return tensorflow.keras.Model(self.inputs, [self.references.y_source, self.references.pos_flow])


class VxmAffine(LoadableModel):
    """
    VoxelMorph network for linear (affine) registration between two images.
    """

    @store_config_args
    def __init__(self, inshape, enc_nf, blurs=[1], rigid=False):
        """
        Parameters:
            inshape: Input shape. e.g. (192, 192, 192)
            enc_nf: List of encoder filters. e.g. [16, 32, 32, 32]
            blurs: List of gaussian blur kernel levels for inputs. Default is [1].
            rigid: Require rigid registration (not fully tested). Default is False.
        """

        # ensure correct dimensionality
        ndims = len(inshape)
        assert ndims in [1, 2, 3], 'ndims should be one of 1, 2, or 3. found: %d' % ndims

        # configure base encoder CNN
        Conv = getattr(KL, 'Conv%dD' % ndims)   
        basenet = Sequential()
        for nf in enc_nf:
            basenet.add(Conv(nf, kernel_size=3, padding='same', kernel_initializer='he_normal', strides=2))
            basenet.add(LeakyReLU(0.2))
        
        # dense layer to affine matrix
        basenet.add(KL.Flatten())

        if rigid:
            print('Warning: rigid registration has not been fully tested')
            basenet.add(KL.Dense(ndims * 2))
            basenet.add(layers.EulerToAffine())
        else:
            basenet.add(KL.Dense(ndims * (ndims + 1)))

        # inputs
        source = Input(shape=[*inshape, 1])
        target = Input(shape=[*inshape, 1])

        # build net with multi-scales
        affines = []
        scale_source = source
        for blur in blurs:
            # set input and blur using gaussian kernel  
            source_blur = gaussian_blur(scale_source, blur, ndims)
            target_blur = gaussian_blur(target, blur, ndims)
            x_in = concatenate([source_blur, target_blur])

            # apply base net to affine
            affine = basenet(x_in)
            affines.append(affine)
 
            # spatial transform using affine matrix
            y_source = layers.SpatialTransformer(add_identity=(not rigid))([source_blur, affine])

            # provide new input for next scale
            if len(blurs) > 1:
                scale_source = layers.SpatialTransformer(add_identity=(not rigid))([scale_source, affine])

        # initialize the keras model
        super().__init__(name='affine_net', inputs=[source, target], outputs=[y_source])

        # cache affines
        self.references = LoadableModel.ReferenceContainer()
        self.references.affines = affines

    def get_predictor_model(self):
        """
        Extracts a predictor model from the VxmAffine that directly outputs the
        computed affines instead of the transformed source image.
        """
        return tensorflow.keras.Model(self.inputs, self.references.affines)


class InstanceTrainer:
    """
    VoxelMorph network to perform instance-specific optimization.
    """

    def __init__(self, inshape, warp):
        source = tensorflow.keras.layers.Input(shape=inshape)
        target = tensorflow.keras.layers.Input(shape=inshape)
        nullwarp = tensorflow.keras.layers.Input(shape=warp.shape[1:])  # this is basically ignored by LocalParamWithInput
        flow_layer = vxm.layers.LocalParamWithInput(shape=warp.shape[1:])
        flow = flow_layer(nullwarp)
        y = vxm.layers.SpatialTransformer()([source, flow])

        # initialize the keras model
        super().__init__(name='instance_net', inputs=[source, target, nullwarp], outputs=[y, flow])

        # initialize weights with original predicted warp
        flow_layer.set_weights(warp)


class ProbAtlasSegmentation(LoadableModel):
    """
    VoxelMorph network to segment images by warping a probabilistic atlas.
    """

    @store_config_args
    def __init__(self,
        inshape,
        enc_nf,
        dec_nf,
        nb_labels,
        init_mu=None,
        init_sigma=None,
        warp_atlas=True,
        stat_post_warp=True,
        stat_nb_feats=16,
        network_stat_weight=0.001,
        **kwargs):
        """ 
        Parameters:
            inshape: Input shape. e.g. (192, 192, 192)
            enc_nf: List of encoder filters. e.g. [16, 32, 32, 32]
            dec_nf: List of decoder filters. e.g. [32, 32, 32, 32, 32, 16, 16]
            nb_labels: Number of labels in probabilistic atlas.
            init_mu: Optional initialization for gaussian means. Default is None.
            init_sigma: Optional initialization for gaussian sigmas. Default is None.
            stat_post_warp: Computes gaussian stats using the warped atlas. Default is True.
            stat_nb_feats: Number of features in the stats convolutional layer. Default is 16.
            network_stat_weight: Relative weight of the stats learned by the network. Default is 0.001.
            kwargs: Forwarded to the internal VxmDense model.
        """

        # ensure correct dimensionality
        ndims = len(inshape)
        assert ndims in [1, 2, 3], 'ndims should be one of 1, 2, or 3. found: %d' % ndims

        # build warp network
        vxm_model = VxmDense(inshape, enc_nf, dec_nf, src_feats=nb_labels, **kwargs)

        # extract necessary layers from the network
        # important to note that we're warping the atlas to the image in this case and
        # we'll swap the input order later
        atlas, image = vxm_model.inputs
        warped_atlas = vxm_model.references.y_source if warp_atlas else atlas
        flow = vxm_model.references.pos_flow

        # compute stat using the warped atlas (or not)
        if stat_post_warp:
            assert warp_atlas, 'must enable warp_atlas if computing stat post warp'
            combined = concatenate([warped_atlas, image])
        else:
            # use last convolution in the unet before the flow convolution
            combined = vxm_model.references.unet_model.layers[-2].output

        # convolve into nlabel-stat volume
        conv = conv_block(combined, stat_nb_feats)
        conv = conv_block(conv, nb_labels)

        Conv = getattr(KL, 'Conv%dD' % ndims)
        weaknorm = RandomNormal(mean=0.0, stddev=1e-5)

        # convolve into mu and sigma volumes
        stat_mu_vol = Conv(nb_labels, kernel_size=3, name='mu_vol', kernel_initializer=weaknorm, bias_initializer=weaknorm)(conv)
        stat_logssq_vol = Conv(nb_labels, kernel_size=3, name='logsigmasq_vol', kernel_initializer=weaknorm, bias_initializer=weaknorm)(conv)
        
        # pool to get 'final' stat
        stat_mu = tensorflow.keras.layers.GlobalMaxPooling3D()(stat_mu_vol)
        stat_logssq = tensorflow.keras.layers.GlobalMaxPooling3D()(stat_logssq_vol)

        # combine mu with initialization
        if init_mu is not None: 
            init_mu = np.array(init_mu)
            stat_mu = Lambda(lambda x: network_stat_weight * x + init_mu, name='comb_mu')(stat_mu)
        
        # combine sigma with initialization
        if init_sigma is not None: 
            init_logsigmasq = np.array([2 * np.log(f) for f in init_sigma])
            stat_logssq = Lambda(lambda x: network_stat_weight * x + init_logsigmasq, name='comb_sigma')(stat_logssq)

        # unnorm loglike
        def unnorm_loglike(I, mu, logsigmasq, use_log=True):
            P = tf.distributions.Normal(mu, K.exp(logsigmasq/2))
            return P.log_prob(I) if use_log else P.prob(I)
        uloglhood = KL.Lambda(lambda x:unnorm_loglike(*x), name='unsup_likelihood')([image, stat_mu, stat_logssq])

        # compute data loss as a layer, because it's a bit easier than outputting a ton of things
        def logsum(prob_ll, atl):
            # safe computation using the log sum exp trick (NOTE: this does not normalize p)
            # https://www.xarg.org/2016/06/the-log-sum-exp-trick-in-machine-learning
            logpdf = prob_ll + K.log(atl + K.epsilon())
            alpha = tf.reduce_max(logpdf, -1, keepdims=True)
            return alpha + tf.log(tf.reduce_sum(K.exp(logpdf-alpha), -1, keepdims=True) + K.epsilon())
        loss_vol = Lambda(lambda x: logsum(*x))([uloglhood, warped_atlas])

        # initialize the keras model
        super().__init__(inputs=[image, atlas], outputs=[loss_vol, flow])

        # cache pointers to layers and tensors for future reference
        self.references = LoadableModel.ReferenceContainer()
        self.references.vxm_model = vxm_model
        self.references.uloglhood = uloglhood
        self.references.stat_mu = stat_mu
        self.references.stat_logssq = stat_logssq

    def get_predictor_model(self):
        """
        Extracts a predictor model from the ProbAtlasSegmentation model that directly
        outputs the gaussian stats and warp field.
        """
        outputs = [
            self.references.uloglhood,
            self.references.stat_mu,
            self.references.stat_logssq,
            self.outputs[-1]
        ]
        return tensorflow.keras.Model(self.inputs, outputs)


class TemplateCreation(LoadableModel):
    """
    VoxelMorph network to generate an unconditional template image.
    """

    @store_config_args
    def __init__(self, inshape, enc_nf, dec_nf, mean_cap=100, **kwargs):
        """ 
        Parameters:
            inshape: Input shape. e.g. (192, 192, 192)
            enc_nf: List of encoder filters. e.g. [16, 32, 32, 32]
            dec_nf: List of decoder filters. e.g. [32, 32, 32, 32, 32, 16, 16]
            mean_cap: Cap for mean stream. Default is 100.
            kwargs: Forwarded to the internal VxmDense model.
        """

        # warp model
        vxm_model = VxmDense(inshape, enc_nf, dec_nf, bidir=True, **kwargs)

        # pre-warp (atlas) model
        atlas = layers.LocalParamWithInput(name='atlas', shape=[*inshape, 1], mult=1.0,
                                   initializer=RandomNormal(mean=0.0, stddev=1e-7))(vxm_model.inputs[0])
        prewarp_model = tensorflow.keras.Model(vxm_model.inputs[0], atlas)

        # stack models
        stacked = ne.utils.stack_models([prewarp_model, vxm_model], [[0]])

        # extract tensors from stacked model
        y_source = stacked.get_layer('transformer').get_output_at(-1)
        y_target = stacked.get_layer('neg_transformer').get_output_at(-1)
        pos_flow = stacked.get_layer('transformer').get_input_at(-1)[1]
        neg_flow = stacked.get_layer('neg_transformer').get_input_at(-1)[1]

        # get mean stream of negative flow
        mean_stream = ne.layers.MeanStream(name='mean_stream', cap=mean_cap)(neg_flow)

        # initialize the keras model
        outputs = [y_source, y_target, mean_stream, pos_flow]
        super().__init__(inputs=vxm_model.inputs, outputs=outputs)

        # cache pointers to important layers and tensors for future reference
        self.references = LoadableModel.ReferenceContainer()
        self.references.atlas_layer = stacked.get_layer('atlas')
        self.references.atlas_tensor = self.references.atlas_layer.get_output_at(-1)


class ConditionalTemplateCreation(LoadableModel):
    """
    VoxelMorph network to generate an conditional template image.
    """

    @store_config_args
    def __init__(self,
        inshape,
        pheno_input_shape,
        enc_nf,
        dec_nf,
        src_feats=1,
        conv_image_shape=None,
        conv_size=3,
        conv_nb_levels=5,
        conv_nb_features=32,
        extra_conv_layers=0,
        use_mean_stream=True,
        mean_cap=100,
        use_stack=True,
        templcondsi=False,
        templcondsi_init=None,
        **kwargs):
        """ 
        Parameters:
            inshape: Input shape. e.g. (192, 192, 192)
            pheno_input_shape: Pheno data input shape. e.g. (2)
            enc_nf: List of encoder filters. e.g. [16, 32, 32, 32]
            dec_nf: List of decoder filters. e.g. [32, 32, 32, 32, 32, 16, 16]
            src_feats: Number of source (atlas) features. Default is 1.
            conv_image_shape: Intermediate phenotype image shape. Default is inshape with conv_nb_features.
            conv_size: Atlas generator convolutional kernel size. Default is 3.
            conv_nb_levels: Number of levels in atlas generator unet. Default is 5.
            conv_nb_features: Number of features in atlas generator convolutions. Default is 32.
            extra_conv_layers: Number of extra convolutions after unet in atlas generator. Default is 0.
            use_mean_stream: Return mean stream layer for training. Default is True.
            mean_cap: Cap for mean stream. Default is 100.
            use_stack: Stack models instead of combining manually. Default is True.
            templcondsi: Default is False.
            templcondsi_init: Default is None.
            kwargs: Forwarded to the internal VxmDense model.
        """

        # warp model
        vxm_model = VxmDense(inshape, enc_nf, dec_nf, bidir=True, src_feats=src_feats, **kwargs)

        if not use_stack:
            outputs = vxm_model.outputs + [vxm_model.references.pos_flow, vxm_model.references.neg_flow]
            vxm_model = Model(inputs=vxm_model.inputs, outputs=outputs)

        if conv_image_shape is None:
            conv_image_shape = (*inshape, conv_nb_features)

        # build initial dense pheno to image shape model
        pheno_input = KL.Input(pheno_input_shape, name='pheno_input')
        pheno_dense = KL.Dense(np.prod(conv_image_shape), activation='elu')(pheno_input)
        pheno_reshaped = KL.Reshape(conv_image_shape)(pheno_dense)
        pheno_init_model = tensorflow.keras.models.Model(pheno_input, pheno_reshaped)

        # build model to decode reshaped pheno
        pheno_decoder_model = ne.models.conv_dec(conv_nb_features, conv_image_shape, conv_nb_levels, conv_size,
                                                 nb_labels=conv_nb_features, final_pred_activation='linear',
                                                 input_model=pheno_init_model, name='atlas_decoder')

        # add extra convolutions
        Conv = getattr(KL, 'Conv%dD' % len(inshape))
        last = pheno_decoder_model.output
        for n in range(extra_conv_layers):
            last = Conv(conv_nb_features, kernel_size=conv_size, padding='same', name='atlas_extra_conv_%d' % n)(last)

        # final convolution to get atlas features
        atlas_gen = Conv(src_feats, kernel_size=3, padding='same', name='atlas_gen',
                         kernel_initializer=RandomNormal(mean=0.0, stddev=1e-7),
                         bias_initializer=RandomNormal(mean=0.0, stddev=1e-7))(last)

        # atlas input layer
        atlas_input = KL.Input([*inshape, src_feats], name='atlas_input')

        if templcondsi:
            atlas_tensor = KL.Add(name='atlas_tmp')([atlas_input, pout])
            # change first channel to be result from seg with another add layer
            tmp_layer = KL.Lambda(lambda x: K.softmax(x[..., 1:]))(atlas_tensor)
            conv_layer = Conv(1, kernel_size=1, padding='same', use_bias=False, name='atlas_gen', kernel_initializer=RandomNormal(mean=0, stddev=1e-5))
            x_img = conv_layer(tmp_layer)
            if templcondsi_init is not None:
                weights = conv_layer.get_weights()
                weights[0] = templcondsi_init.reshape(weights[0].shape)
                conv_layer.set_weights(weights)
            atlas_tensor = KL.Lambda(lambda x: K.concatenate([x[0], x[1][...,1:]]), name='atlas')([x_img, atlas_tensor])
        else:
            atlas = KL.Add(name='atlas')([atlas_input, atlas_gen])

        # build complete pheno to atlas model
        pheno_model = tensorflow.keras.models.Model([pheno_decoder_model.input, atlas_input], atlas)

        # stacked input list
        inputs = pheno_model.inputs + [vxm_model.inputs[1]]

        if use_stack:
            stacked = ne.utils.stack_models([pheno_model, vxm_model], [[0]])
            y_source = stacked.get_layer('transformer').get_output_at(-1)
            pos_flow = stacked.get_layer('transformer').get_input_at(-1)[1]
            neg_flow = stacked.get_layer('neg_transformer').get_input_at(-1)[1]
        else:
            y_source, _, _, pos_flow, neg_flow = vxm_model(pheno_model.outputs + [vxm_model.inputs[1]])

        if use_mean_stream:
            # get mean stream from negative flow
            mean_stream = ne.layers.MeanStream(name='mean_stream', cap=mean_cap)(neg_flow)
            outputs = [y_source, mean_stream, pos_flow, pos_flow]
        else:
            outputs = [y_source, pos_flow, pos_flow]

        # initialize the keras model
        super().__init__(inputs=inputs, outputs=outputs)


class HyperparameterTuning(LoadableModel):
    """
    VoxelMorph network to tune hyperparameter weights.
    """
    @store_config_args
    def __init__(self,
        inshape,
        enc_nf,
        dec_nf,
        nb_hyperparams=1,
        src_feats=1,
        conv_image_shape=None,
        conv_size=3,
        conv_nb_levels=2,
        conv_nb_features=8,
        extra_conv_layers=3,
        **kwargs):
        """ 
        Parameters:
            inshape: Input shape. e.g. (192, 192, 192)
            enc_nf: List of encoder filters. e.g. [16, 32, 32, 32]
            dec_nf: List of decoder filters. e.g. [32, 32, 32, 32, 32, 16, 16]
            nb_hyperparams: Number of hyperparameters to train. Default is 1.
            src_feats: Number of source features. Default is 1.
            conv_image_shape: Intermediate phenotype image shape. Default is half of inshape.
            conv_size: Atlas generator convolutional kernel size. Default is 3.
            conv_nb_levels: Number of levels in atlas generator unet. Default is 2.
            conv_nb_features: Number of features in atlas generator convolutions. Default is 8.
            extra_conv_layers: Number of extra convolutions after unet in atlas generator. Default is 3.
            kwargs: Forwarded to the internal VxmDense model.
        """

        # by default, use 2x downsampled input image shape as initial param image
        if conv_image_shape is None:
            conv_image_shape = (*(np.array(inshape) / 2.0).astype(int), 1)

        # build initial dense param to image shape model
        param_input = KL.Input([nb_hyperparams], name='param_input')
        param_dense = KL.Dense(np.prod(conv_image_shape), activation='elu')(param_input)
        param_reshaped = KL.Reshape(conv_image_shape)(param_dense)
        param_init_model = tf.keras.models.Model(param_input, param_reshaped)

        # build model to decode reshaped param
        param_decoder_model = ne.models.conv_dec(conv_nb_features, conv_image_shape, conv_nb_levels, conv_size,
                                                 nb_labels=conv_nb_features, final_pred_activation='linear',
                                                 input_model=param_init_model, name='param_decoder')

        # add extra convolutions
        Conv = getattr(KL, 'Conv%dD' % len(inshape))
        last = param_decoder_model.output
        for n in range(extra_conv_layers):
            last = Conv(conv_nb_features, kernel_size=conv_size, padding='same', name='atlas_extra_conv_%d' % n)(last)

        # final convolution to get to final features
        param_vol = Conv(conv_nb_features, kernel_size=3, padding='same', name='param_vol',
                         kernel_initializer=RandomNormal(mean=0.0, stddev=1e-7),
                         bias_initializer=RandomNormal(mean=0.0, stddev=1e-7))(last)

        # build complete hyperparam model
        hyperparam_model = tf.keras.models.Model(param_input, param_vol)

        # warp model
        vxm_model = VxmDense(inshape, enc_nf, dec_nf, input_model=hyperparam_model, **kwargs)

        # initialize the keras model
        super().__init__(inputs=vxm_model.inputs, outputs=vxm_model.outputs)

        # cache pointers to layers and tensors for future reference
        self.references = LoadableModel.ReferenceContainer()
        self.references.param_input = param_input
        self.references.vxm_model = vxm_model

    def get_predictor_model(self):
        """
        Extracts a predictor model from the VxmDense that directly outputs the warped image and 
        final diffeomorphic warp field (instead of the non-integrated flow field used for training).
        """
        vxm_model_refs = self.references.vxm_model.references
        return tf.keras.Model(self.inputs, [vxm_model_refs.y_source, vxm_model_refs.pos_flow])


class VxmDenseSegSemiSupervised(LoadableModel):
    """
    VoxelMorph network for (semi-supervised) nonlinear registration between two images.
    """

    @store_config_args
    def __init__(self, inshape, enc_nf, dec_nf, nb_labels, int_steps=7, int_downsize=2, seg_downsize=2):
        """
        Parameters:
            inshape: Input shape. e.g. (192, 192, 192)
            enc_nf: List of encoder filters. e.g. [16, 32, 32, 32]
            dec_nf: List of decoder filters. e.g. [32, 32, 32, 32, 32, 16, 16]
            nb_labels: Number of labels used for ground truth segmentations.
            int_steps: Number of flow integration steps. The warp is non-diffeomorphic when this value is 0.
            int_downsize: Integer specifying the flow downsample factor for vector integration. The flow field
                is not downsampled when this value is 1.
            seg_downsize: Interger specifying the downsampled factor of the segmentations. Default is 2.
        """

        # configure base voxelmorph network
        vxm_model = VxmDense(inshape, enc_nf, dec_nf, int_steps=int_steps, int_downsize=int_downsize)

        # configure downsampled seg input layer
        inshape_downsized = (np.array(inshape) / seg_downsize).astype(int)
        seg_src = Input(shape=(*inshape_downsized, nb_labels))

        # configure warped seg output layer
        seg_flow = layers.RescaleTransform(1 / seg_downsize, name='seg_resize')(vxm_model.references.pos_flow)
        y_seg = layers.SpatialTransformer(interp_method='linear', indexing='ij', name='seg_transformer')([seg_src, seg_flow])

        # initialize the keras model
        inputs = vxm_model.inputs + [seg_src]
        outputs = vxm_model.outputs + [y_seg]
        super().__init__(inputs=inputs, outputs=outputs)


class VxmDenseSurfaceSemiSupervised(LoadableModel):
    """
    VoxelMorph network for semi-supervised nonlinear registration aided by surface point registration.
    """

    @store_config_args
    def __init__(self,
        inshape,
        enc_nf,
        dec_nf,
        nb_surface_points,
        nb_labels_sample,
        sdt_vol_resize=1,
        surf_bidir=True,
        **kwargs):
        """ 
        Parameters:
            inshape: Input shape. e.g. (192, 192, 192)
            enc_nf: List of encoder filters. e.g. [16, 32, 32, 32]
            dec_nf: List of decoder filters. e.g. [32, 32, 32, 32, 32, 16, 16]
            nb_surface_points: Number of surface points to warp.
            nb_labels_sample: Number of labels to sample.
            sdt_vol_resize: Resize factor of distance transform. Default is 1.
            surf_bidir: Train with bidirectional surface warping. Default is True.
            kwargs: Forwarded to the internal VxmDense model.
        """

        sdt_shape = [int(f * sdt_vol_resize) for f in inshape]
        surface_points_shape = [nb_surface_points, len(inshape) + 1]
        single_pt_trf = lambda x: point_spatial_transformer(x, sdt_vol_resize=sdt_vol_resize)

        # vm model
        dense = VxmDense(inshape, enc_nf, dec_nf, bidir=True, **kwargs)

        # surface inputs and invert atlas_v for inverse transform to get final 'atlas surface'
        atl_surf_input = tensorflow.keras.layers.Input(surface_points_shape, name='atl_surface_input')

        # warp atlas surface
        # NOTE: pos diffflow is used to define an image moving x --> A, but when moving points, it moves A --> x
        warped_atl_surf_pts = Lambda(single_pt_trf, name='warped_atl_surface')([atl_surf_input, dense.references.pos_flow])

        # get value of dt_input *at* warped_atlas_surface
        subj_dt_input = tensorflow.keras.layers.Input([*sdt_shape, nb_labels_sample], name='subj_dt_input')
        subj_dt_value = Lambda(value_at_location, name='hausdorff_subj_dt')([subj_dt_input, warped_atl_surf_pts])

        if surf_bidir:
            # go the other way and warp subject to atlas
            subj_surf_input = tensorflow.keras.layers.Input(surface_points_shape, name='subj_surface_input')
            warped_subj_surf_pts = Lambda(single_pt_trf, name='warped_subj_surface')([subj_surf_input, dense.references.neg_flow])

            atl_dt_input = tensorflow.keras.layers.Input([*sdt_shape, nb_labels_sample], name='atl_dt_input')
            atl_dt_value = Lambda(value_at_location, name='hausdorff_atl_dt')([atl_dt_input, warped_subj_surf_pts])

            inputs  = [*dense.inputs, subj_dt_input, atl_dt_input, subj_surf_input, atl_surf_input]
            outputs = [*dense.outputs, subj_dt_value, atl_dt_value]

        else:
            inputs  = [*dense.inputs, subj_dt_input, atl_surf_input]
            outputs = [*dense.outputs, subj_dt_value]

        # initialize the keras model
        super().__init__(inputs=inputs, outputs=outputs)


def transform(
        inshape,
        interp_method='linear',
        indexing='ij',
        nb_feats=1,
        int_steps=0,
        int_method='ss',
        vel_resize=1,
        **kwargs  # kwargs are for VecInt
    ):
    """
    Simple transform model.

    NOTE: This is essentially a wrapper for neuron.utils.transform.
    TODO: Have a new 'Transform' layer that is specific to VoxelMorph that can be a deformation or something else.
    """
    ndims = len(inshape)

    # nn warp model
    scan_input = Input((*inshape, nb_feats), name='scan_input')
    trf_input = Input((*[int(f*vel_resize) for f in inshape], ndims) , name='trf_input')

    if int_steps > 0:
        trf = ne.layers.VecInt(method=int_method, name='trf-int', int_steps=int_steps, **kwargs)(trf_input)
        trf = layers.RescaleTransform(1 / vel_resize, name='flow')(trf)
    else:
        trf = trf_input

    # note the nearest neighbour interpolation method
    # use xy indexing when Guha's original code switched x and y dimensions
    nn_output = layers.SpatialTransformer(interp_method=interp_method, indexing=indexing)
    nn_spatial_output = nn_output([scan_input, trf])
    return Model([scan_input, trf_input], nn_spatial_output)


def transform_nn(inshape, **kwargs):
    """
    Simple transform model for nearest-neighbor based transformation.
    """
    return transform(inshape, interp_method='nearest', **kwargs)


def transform_affine(inshape):
    """
    Transformer network that applies an affine registration matrix to an image.
    """
    source = Input((*inshape, 1))
    affine = Input((12,))
    aligned = layers.SpatialTransformer()([source, affine])
    return Model([source, affine], aligned)


def conv_block(x, nfeat, strides=1):
    """
    Specific convolutional block followed by leakyrelu for unet.
    """
    ndims = len(x.get_shape()) - 2
    assert ndims in (1, 2, 3), "ndims should be one of 1, 2, or 3. found: %d" % ndims
    Conv = getattr(KL, 'Conv%dD' % ndims)

    convolved = Conv(nfeat, kernel_size=3, padding='same', kernel_initializer='he_normal', strides=strides)(x)
    return LeakyReLU(0.2)(convolved)


def upsample_block(x, connection):
    """
    Specific upsampling and concatenation layer for unet.
    """
    ndims = len(x.get_shape()) - 2
    assert ndims in (1, 2, 3), "ndims should be one of 1, 2, or 3. found: %d" % ndims
    UpSampling = getattr(KL, 'UpSampling%dD' % ndims)

    upsampled = UpSampling()(x)
    return concatenate([upsampled, connection])


def unet(inshape, enc_nf, dec_nf, src_feats=1, trg_feats=1, input_model=None):
    """ 
    Constructs a simple unet architecture.

    Parameters:
        inshape: Input shape. e.g. (256, 256, 256)
        enc_nf: List of encoder filters. e.g. [16, 32, 32, 32]
        dec_nf: List of decoder filters. e.g. [32, 32, 32, 32, 8, 8]
        src_feats: Number of source image features. Default is 1.
        trg_feats: Number of target image features. Default is 1.
        input_model: Model to concat with input layer.
    """

    # configure inputs
    concat = [
        Input(shape=(*inshape, src_feats)),
        Input(shape=(*inshape, trg_feats))
    ]
    inputs = concat.copy()

    if input_model is not None:
        concat += input_model.outputs
        inputs += input_model.inputs

    # configure encoder (down-sampling path)
    enc_layers = [concatenate(concat)]
    for nf in enc_nf:
        enc_layers.append(conv_block(enc_layers[-1], nf, strides=2))

    # configure decoder (up-sampling path)
    x = enc_layers.pop()
    for nf in dec_nf[:len(enc_nf)]:
        x = conv_block(x, nf, strides=1)
        x = upsample_block(x, enc_layers.pop())

    # now we take care of the remaining convolutions
    for i, nf in enumerate(dec_nf[len(enc_nf):]):
        x = conv_block(x, nf, strides=1)

    return Model(inputs=inputs, outputs=[x])


def gaussian_blur(tensor, level, ndims):
    """
    Blurs a tensor using a gaussian kernel (if level=1, then do nothing).
    """
    if level > 1:
        sigma = (level-1) ** 2
        blur_kernel = ne.utils.gaussian_kernel([sigma] * ndims)
        blur_kernel = tf.reshape(blur_kernel, blur_kernel.shape.as_list() + [1, 1])
        conv = lambda x: tf.nn.conv3d(x, blur_kernel, [1, 1, 1, 1, 1], 'SAME')
        return KL.Lambda(conv)(tensor)
    elif level == 1:
        return tensor
    else:
        raise ValueError('Gaussian blur level must not be less than 1')


def value_at_location(x, single_vol=False, single_pts=False, force_post_absolute_val=True):
    """
    Extracts value at given point.
    """
    
    # vol is batch_size, *vol_shape, nb_feats
    # loc_pts is batch_size, nb_surface_pts, D or D+1
    vol, loc_pts = x

    fn = lambda y: ne.utils.interpn(y[0], y[1])
    z = tf.map_fn(fn, [vol, loc_pts], dtype=tf.float32)

    if force_post_absolute_val:
        z = K.abs(z)
    return z


def point_spatial_transformer(x, single=False, sdt_vol_resize=1):
    """
    Transforms surface points with a given deformation.
    Note that the displacement field that moves image A to image B will be "in the space of B".
    That is, `trf(p)` tells you "how to move data from A to get to location `p` in B". 
    Therefore, that same displacement field will warp *landmarks* in B to A easily 
    (that is, for any landmark `L(p)`, it can easily find the appropriate `trf(L(p))` via interpolation.
    """

    # surface_points is a N x D or a N x (D+1) Tensor
    # trf is a *volshape x D Tensor
    surface_points, trf = x
    trf = trf * sdt_vol_resize
    surface_pts_D = surface_points.get_shape().as_list()[-1]
    trf_D = trf.get_shape().as_list()[-1]
    assert surface_pts_D in [trf_D, trf_D + 1]

    if surface_pts_D == trf_D + 1:
        li_surface_pts = K.expand_dims(surface_points[..., -1], -1)
        surface_points = surface_points[..., :-1]

    # just need to interpolate.
    # at each location determined by surface point, figure out the trf...
    # note: if surface_points are on the grid, gather_nd should work as well
    fn = lambda x: ne.utils.interpn(x[0], x[1])
    diff = tf.map_fn(fn, [trf, surface_points], dtype=tf.float32)
    ret = surface_points + diff

    if surface_pts_D == trf_D + 1:
        ret = tf.concat((ret, li_surface_pts), -1)

    return ret


# make ModelCheckpointParallel directly available from vxm
ModelCheckpointParallel = ne.callbacks.ModelCheckpointParallel

# make neuron.utils.transform directly available from vxm
neuron_transform = ne.utils.transform
